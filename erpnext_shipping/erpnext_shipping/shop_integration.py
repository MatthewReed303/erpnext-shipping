"""Starshipit for the Shop app: live rates at checkout, and store orders handed to Shipments.

Both are registered in hooks.py and only run when the Shop app calls them:

- shop_shipping_rate_providers: rates for the shopper's cart and address. Parcels come from
  the items' weights and Parcel Templates, packed the same way as a Shipment's parcels
  (erpnext_shipping.utils.parcels_for_items). Answers are cached for 15 minutes, since checkout
  asks again as the shopper types and Starshipit allows two requests a second; failures aren't.
- shop_fulfillment_providers["starshipit"]: when a paid order is sent for fulfilment, it is
  delivered (Delivery Note) and a draft Shipment is made with the parcels and the service the
  customer chose. You check the parcels, submit, and create the Starshipit order as usual.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate

from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import (
	STARSHIPIT_PROVIDER,
	StarshipitUtils,
	kg_per_unit,
)
from erpnext_shipping.erpnext_shipping.utils import parcels_for_items

RATE_CACHE_SECONDS = 15 * 60
# Starshipit statuses before the courier has the parcel: a printed label is not shipped yet
NOT_YET_SHIPPED = {"", "unshipped", "saved", "new", "printed"}
OPTION_PREFIX = "starshipit:"


# --- checkout rates ---------------------------------------------------------------


def settings():
	return frappe.get_cached_doc("Starshipit")


def store_rates_enabled() -> bool:
	s = settings()
	return bool(s.enabled and s.get("shop_checkout_rates") and s.get("sender_address"))


def checkout_rates(context) -> dict | None:
	"""Starshipit services and prices for a cart going to an address (Shop rate provider).

	None when store rates are switched off, so Shop knows Starshipit is not in use."""
	if not store_rates_enabled():
		return None
	s = settings()
	address = context.address
	sender = sender_address()
	lines, restricted = cart_lines(context.cart_items)
	if not lines:
		return None  # nothing in the cart is sent
	if address.country and address.country != sender.country and restricted:
		return {
			"options": [],
			"messages": [
				_("{0} can only be sent within {1}. Remove it to ship overseas, or contact us.").format(
					", ".join(restricted), sender.country
				)
			],
		}
	try:
		parcels = parcels_for_items(lines, None, s.default_parcel_template)
	except frappe.ValidationError:
		frappe.clear_messages()
		parcels = []
	if not parcels:
		frappe.log_error(title="Store shipping: items have no weight", message=json.dumps(context.cart_items))
		return {
			"options": [],
			"messages": [_("We can't work out shipping for these items. Please contact us.")],
		}
	destination = frappe._dict(
		address_line1=address.address_line1,
		address_line2=address.address_line2,
		city=address.city,
		state=address.state,
		pincode=(address.pincode or "").replace(" ", ""),
		country=address.country,
		country_code=address.country_code or "",
	)
	key = cache_key(sender, destination, parcels)
	services = frappe.cache.get_value(key)
	if services is None:
		try:
			services = [
				dict(service)
				for service in StarshipitUtils().get_available_services(
					pickup_address=sender,
					delivery_address=destination,
					parcels=parcels,
					value_of_goods=context.subtotal,
					checkout=True,
				)
			]
		except frappe.ValidationError:
			# not cached, so the next quote asks Starshipit again
			frappe.clear_messages()
			return {
				"options": [],
				"messages": [
					_("Live shipping prices aren't available right now. Please try again in a moment.")
				],
			}
		frappe.cache.set_value(key, services, expires_in_sec=RATE_CACHE_SECONDS)
	allowed = allowed_services()
	options = []
	for service in services:
		if allowed and service.get("service_code") not in allowed:
			continue
		options.append(
			{
				"id": OPTION_PREFIX + service["service_id"],
				"label": service_label(service),
				"amount": flt(service.get("total_price"), 2),
				"tax_included": bool(s.get("rates_include_tax")),
				"carrier": service.get("carrier"),
				"service": service.get("service_name"),
				"meta": {
					"service_id": service["service_id"],
					"carrier_code": service.get("carrier_code"),
					"service_code": service.get("service_code"),
					"service_name": service.get("service_name"),
				},
			}
		)
	return {"options": options}


def service_label(service: dict) -> str:
	carrier, name = service.get("carrier") or "", service.get("service_name") or ""
	if carrier and carrier.lower() not in name.lower():
		return f"{carrier} {name}".strip()
	return name or carrier


def allowed_services() -> set[str]:
	return {line.strip() for line in (settings().get("checkout_services") or "").splitlines() if line.strip()}


def sender_address() -> frappe._dict:
	from erpnext_shipping.erpnext_shipping.utils import get_address

	return get_address(settings().sender_address)


def cart_lines(items: list[dict]) -> tuple[list[dict], list[str]]:
	"""Item lines for parcels_for_items, and the names of items that can't leave the country."""
	lines, restricted = [], []
	meta = frappe.get_meta("Item")
	fields = ["item_name", "weight_per_unit", "weight_uom", "is_stock_item"]
	for field in ("shipping_parcel_template", "ships_in_own_box", "ships_domestic_only"):
		if meta.has_field(field):
			fields.append(field)
	for row in items:
		item = frappe.db.get_value("Item", row["item_code"], fields, as_dict=True)
		if not item:
			continue
		if item.get("ships_domestic_only"):
			restricted.append(item.item_name)
		if not item.is_stock_item and not item.weight_per_unit:
			continue  # a service, not something in the parcel
		lines.append(
			frappe._dict(
				item_code=row["item_code"],
				qty=flt(row["qty"]),
				kg=kg_per_unit(item.weight_per_unit, item.weight_uom) or 0,
				template=item.get("shipping_parcel_template"),
				own_box=cint(item.get("ships_in_own_box")),
			)
		)
	return lines, restricted


def cache_key(sender, destination, parcels) -> str:
	raw = json.dumps(
		[
			[sender.pincode, sender.city, sender.country],
			[destination.address_line1, destination.city, destination.pincode, destination.country],
			parcels,
		],
		sort_keys=True,
		default=str,
	)
	return "starshipit_rates:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


# --- fulfilment --------------------------------------------------------------------

try:
	from shop.fulfillment.provider import FulfillmentProvider
except ImportError:  # Shop is not installed
	FulfillmentProvider = object


class StarshipitFulfillment(FulfillmentProvider):
	key = "starshipit"
	label = "Starshipit (Shipment)"

	def is_configured(self) -> bool:
		s = settings()
		return bool(
			s.enabled and s.get("sender_address") and s.get("pickup_contact") and pickup_phone_valid()
		)

	def setup_hint(self) -> str:
		return _(
			"Enable Starshipit and set the Ship From Address and a Pickup Contact whose phone starts with + and "
			"the country code, in Starshipit settings."
		)

	def create_shipment(self, order, items: list[dict]) -> dict:
		shipment = create_shipment_for_order(order.name)
		return {
			"status": "Accepted",
			"external_id": shipment,
			"raw": {"shipment": shipment},
		}

	def fetch_status(self, fulfillment) -> dict:
		if not fulfillment.external_id or not frappe.db.exists("Shipment", fulfillment.external_id):
			return {"status": fulfillment.status}
		shipment = frappe.db.get_value(
			"Shipment",
			fulfillment.external_id,
			["docstatus", "awb_number", "carrier", "tracking_url", "tracking_status", "tracking_status_info"],
			as_dict=True,
		)
		if shipment.docstatus == 2:
			return {"status": "Cancelled"}
		result = {"raw": shipment, "carrier": shipment.carrier}
		info = (shipment.tracking_status_info or "").split(":")[0].strip().lower()
		if shipment.tracking_status == "Delivered":
			result["status"] = "Delivered"
		elif shipment.tracking_status == "In Progress" and info not in NOT_YET_SHIPPED:
			# manifested, or the courier has scanned it
			result["status"] = "Shipped"
		else:
			result["status"] = "Accepted"
		if shipment.awb_number:
			result["tracking_number"] = shipment.awb_number.split(",")[0].strip()
		if shipment.tracking_url:
			result["tracking_url"] = shipment.tracking_url
		return result

	def cancel(self, fulfillment) -> dict:
		"""Cancel the Shipment (and its Starshipit order) and put the stock back: an unbilled Delivery
		Note is cancelled, an invoiced one is returned with ERPNext's return Delivery Note."""
		name = fulfillment.external_id
		if name and frappe.db.exists("Shipment", name):
			doc = frappe.get_doc("Shipment", name)
			notes = [row.delivery_note for row in doc.shipment_delivery_note]
			if doc.docstatus == 0:
				doc.delete(ignore_permissions=True)
			elif doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()  # also deletes or archives the Starshipit order
			for note in notes:
				note_doc = frappe.get_doc("Delivery Note", note)
				if note_doc.docstatus != 1:
					continue
				if not flt(note_doc.per_billed):
					note_doc.flags.ignore_permissions = True
					note_doc.cancel()
				else:
					return_note(note)
		return {"status": "Cancelled"}


def sync_store_fulfillment(shipment: str) -> None:
	"""Bring the store's fulfilment of a Shipment up to date when Starshipit has news of it, so the
	customer hears it has shipped or arrived without waiting for Shop's hourly sync."""
	if "shop" not in frappe.get_installed_apps():
		return
	from shop.fulfillment import service

	for name in frappe.get_all(
		"Shop Fulfillment",
		filters={
			"provider": StarshipitFulfillment.key,
			"external_id": shipment,
			"status": ["in", ("Pending", "Accepted", "Shipped")],
		},
		pluck="name",
	):
		service.sync(name)


def return_note(delivery_note: str) -> str:
	"""ERPNext's return Delivery Note for what an invoiced Delivery Note sent, so the stock is back."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

	returned = make_sales_return(delivery_note)
	returned.flags.ignore_permissions = True
	returned.insert(ignore_permissions=True)
	returned.submit()
	return returned.name


def create_shipment_for_order(order_name: str) -> str:
	"""Deliver the order and make a draft Shipment for it. Returns the Shipment name."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment

	existing = (
		frappe.db.get_value(
			"Shipment Delivery Note",
			{"delivery_note": ["in", delivery_notes(order_name)], "docstatus": ["<", 2]},
			"parent",
		)
		if delivery_notes(order_name)
		else None
	)
	if existing:
		return existing
	delivery_note = deliver(order_name)
	s = settings()
	shipment = make_shipment(delivery_note)
	shipment.pickup_from_type = "Company"
	shipment.pickup_address_name = s.sender_address
	shipment.pickup_contact_person = s.pickup_contact
	set_pickup_contact(shipment, s.pickup_contact)
	shipment.pickup_date = next_business_day()
	shipment.description_of_content = s.get("content_description") or _("Goods")
	shipment.parcel_template = None
	choice = chosen_service(order_name)
	if choice:
		shipment.preferred_service = json.dumps(choice)
	shipment.flags.ignore_permissions = True
	shipment.insert(ignore_permissions=True)
	if choice:
		shipment.add_comment(
			"Info", _("The customer chose {0} at checkout.").format(frappe.bold(choice.get("label") or ""))
		)
	return shipment.name


def set_pickup_contact(shipment, user: str | None) -> None:
	"""The pickup contact's details on the Shipment, as its form fills them from the user."""
	if not user:
		return
	contact = frappe.db.get_value(
		"User", user, ["first_name", "last_name", "email", "phone", "mobile_no"], as_dict=True
	)
	if not contact:
		return
	lines = [
		" ".join(filter(None, [contact.first_name, contact.last_name])),
		contact.email,
		contact.phone or contact.mobile_no,
	]
	shipment.pickup_contact = "<br>".join(filter(None, lines))
	shipment.pickup_contact_email = contact.email


def pickup_phone_valid() -> bool:
	"""The pickup contact's phone is in the form a Shipment needs: + and the country code."""
	import re

	phone = frappe.db.get_value("User", settings().get("pickup_contact"), "phone")
	return bool(phone and re.match(r"^\+(?![\s0])[\d\s]+\d$", phone))


def delivery_notes(order_name: str) -> list[str]:
	return frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": order_name, "docstatus": 1},
		pluck="parent",
		distinct=True,
	)


def deliver(order_name: str) -> str:
	"""A submitted Delivery Note for what the order still has to deliver; when it is all delivered,
	its latest Delivery Note."""
	existing = delivery_notes(order_name)
	if existing and flt(frappe.db.get_value("Sales Order", order_name, "per_delivered")) >= 100:
		return existing[-1]
	try:
		from shop.compat import make_delivery_note
	except ImportError:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
	note = make_delivery_note(order_name)
	note.flags.ignore_permissions = True
	note.insert(ignore_permissions=True)
	note.submit()
	return note.name


def chosen_service(order_name: str) -> dict | None:
	raw = (
		frappe.db.get_value("Sales Order", order_name, "shop_shipping_option")
		if frappe.get_meta("Sales Order").has_field("shop_shipping_option")
		else None
	)
	if not raw:
		return None
	try:
		option = json.loads(raw)
	except ValueError:
		return None
	if not str(option.get("id") or "").startswith(OPTION_PREFIX):
		return {"label": option.get("label"), "amount": option.get("amount")}
	meta = option.get("meta") or {}
	return {
		"label": option.get("label"),
		"amount": option.get("amount"),
		"service_id": meta.get("service_id"),
		"service_name": meta.get("service_name"),
	}


def next_business_day(start: str | None = None) -> str:
	day = getdate(add_days(start or nowdate(), 1))
	while day.weekday() >= 5:
		day = getdate(add_days(day, 1))
	return str(day)


def mark_preferred(services: list[dict], shipment: str | None) -> list[dict]:
	"""Flag the service the customer chose at checkout in the Shipment's service list."""
	if not shipment or not frappe.db.exists("Shipment", shipment):
		return services
	raw = (
		frappe.db.get_value("Shipment", shipment, "preferred_service")
		if frappe.get_meta("Shipment").has_field("preferred_service")
		else None
	)
	if not raw:
		return services
	try:
		choice = json.loads(raw)
	except ValueError:
		return services
	for service in services:
		if service.get("service_provider") == STARSHIPIT_PROVIDER and (
			service.get("service_id") == choice.get("service_id")
			or (choice.get("service_name") and service.get("service_name") == choice.get("service_name"))
		):
			service["is_preferred"] = 1
	return services
