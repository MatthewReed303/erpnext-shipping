# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Starshipit integration.

Starshipit connects to many couriers (NZ Post, CourierPost, Aramex, Australia Post, DHL,
...). This provider follows Starshipit's documented "print outside Starshipit" workflow:

1. quote every service on the account          POST /api/deliveryservices
2. create the order (free, no courier fields)   POST /api/orders
3. set the chosen courier and service           PUT  /api/orders
4. print the label (charged by the courier)     POST /api/orders/shipment
5. manifest the printed order                   POST /api/orders/manifest
6. book a courier pickup                        POST /api/pickup/book
7. keep status up to date                       GET  /api/orders, GET /api/track, webhooks

Printing a label is refused unless "Allow Label Printing" is on; reprints are always
allowed. Developer API access is limited to two requests per second, so requests are paced.

API reference: https://support.starshipit.com/developers/api-reference/starshipit
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_time, get_url, getdate
from frappe.utils.data import get_link_to_form

from erpnext_shipping.erpnext_shipping.utils import show_error_alert

STARSHIPIT_PROVIDER = "Starshipit"
BASE_URL = "https://api.starshipit.com/api"
TIMEOUT = 30
CHECKOUT_TIMEOUT = 8
MIN_REQUEST_INTERVAL = 0.55  # developer API access allows two requests per second
PACING_KEY = "starshipit:last_request_at"
WEIGHT_DECIMALS = 3
SERVICE_ID_SEPARATOR = "|"
WEBHOOK_METHOD = "erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit.webhook"
SIGNATURE_HEADER = "X-Starshipit-Signature"

# Order statuses without a label (Full Order Model status: Unshipped, Saved, Printed or Delivered)
UNPRINTED_STATUSES = ("", "unshipped", "saved", "new")

# ERPNext's Shipment tracking_status only offers In Progress / Delivered / Returned / Lost,
# so Starshipit's detailed status goes to tracking_status_info.
DELIVERED_STATUSES = {"delivered"}
# a cancelled shipment is not on its way, so it gets no tracking_status (the detail stays in tracking_status_info)
NOT_IN_TRANSIT_STATUSES = UNPRINTED_STATUSES + ("cancelled",)

# IANA zone -> Windows zone id, which Book Pickup expects (pickup_timezone_id)
WINDOWS_TIMEZONES = {
	"Pacific/Auckland": "New Zealand Standard Time",
	"Pacific/Chatham": "Chatham Islands Standard Time",
	"Australia/Sydney": "AUS Eastern Standard Time",
	"Australia/Melbourne": "AUS Eastern Standard Time",
	"Australia/Canberra": "AUS Eastern Standard Time",
	"Australia/Brisbane": "E. Australia Standard Time",
	"Australia/Adelaide": "Cen. Australia Standard Time",
	"Australia/Darwin": "AUS Central Standard Time",
	"Australia/Hobart": "Tasmania Standard Time",
	"Australia/Perth": "W. Australia Standard Time",
}


class StarshipitError(frappe.ValidationError):
	pass


class StarshipitNotFoundError(StarshipitError):
	pass


class Starshipit(Document):
	def validate(self):
		if not self.webhook_token:
			self.webhook_token = secrets.token_urlsafe(24)

	@property
	def webhook_url(self):
		if not self.webhook_token:
			return None
		return get_url(f"/api/method/{WEBHOOK_METHOD}?token={self.webhook_token}")


@frappe.whitelist()
def test_connection() -> str:
	"""Check the keys with a read-only request (lists unshipped orders; nothing is created)."""
	frappe.only_for("System Manager")
	StarshipitUtils().request("GET", "/orders/unshipped", params={"limit": 1})
	return _("Connected to Starshipit.")


class StarshipitUtils:
	def __init__(self):
		settings = frappe.get_single("Starshipit")
		self.settings = settings
		self.enabled = settings.enabled
		if not self.enabled:
			link = get_link_to_form("Starshipit", "Starshipit", _("Starshipit Settings"))
			frappe.throw(_("Please enable the Starshipit integration in {0}").format(link))
		self.api_key = settings.get_password("api_key", raise_exception=False)
		self.subscription_key = settings.get_password("subscription_key", raise_exception=False)

	# --- HTTP -----------------------------------------------------------------

	def request(
		self,
		method: str,
		path: str,
		params: dict | None = None,
		payload: dict | None = None,
		log_errors: bool = True,
		timeout: int = TIMEOUT,
		retry: bool = True,
	):
		"""Call the Starshipit API.

		Paced to the account's rate limit across all workers, retried once on HTTP 429 (unless
		retry is off), and failures are logged with params, payload and response but never the
		keys. Raises StarshipitError, StarshipitNotFoundError for HTTP 404.
		"""
		if not self.api_key or not self.subscription_key:
			frappe.throw(_("Set the Starshipit API Key and Subscription Key first."), title=_("Starshipit"))
		for attempt in range(2):
			wait_for_turn()
			try:
				response = requests.request(
					method,
					f"{BASE_URL}{path}",
					params=params,
					json=payload,
					headers={
						"Content-Type": "application/json",
						"StarShipIT-Api-Key": self.api_key,
						"Ocp-Apim-Subscription-Key": self.subscription_key,
					},
					timeout=timeout,
				)
			except requests.RequestException:
				frappe.log_error(title=f"Starshipit {method} {path} did not answer")
				frappe.throw(
					_("Starshipit did not answer. Try again in a moment."),
					StarshipitError,
					title=_("Starshipit"),
				)
			if response.status_code != 429 or attempt or not retry:
				break
			time.sleep(min(cint(response.headers.get("Retry-After")) or 2, 10))
		try:
			data = response.json()
		except ValueError:
			data = None
		if response.ok and not (isinstance(data, dict) and data.get("success") is False):
			return data or {}

		if log_errors:
			frappe.log_error(
				title=f"Starshipit {method} {path} failed (HTTP {response.status_code})",
				message=json.dumps(
					{"params": params, "payload": payload, "response": data or (response.text or "")[:2000]},
					indent=1,
					default=str,
				),
			)
		if response.status_code == 401:
			frappe.throw(_("Starshipit rejected the API keys."), StarshipitError, title=_("Starshipit"))
		if response.status_code == 404:
			frappe.throw(self.format_errors(data, response), StarshipitNotFoundError, title=_("Starshipit"))
		if response.status_code == 429:
			frappe.throw(
				_("Starshipit is limiting requests right now (HTTP 429). Try again in a minute."),
				StarshipitError,
				title=_("Starshipit"),
			)
		frappe.throw(self.format_errors(data, response), StarshipitError, title=_("Starshipit"))

	@staticmethod
	def format_errors(data, response) -> str:
		errors = (data or {}).get("errors") if isinstance(data, dict) else None
		if errors:
			return "<br>".join(
				frappe.utils.escape_html(
					": ".join(filter(None, [cstr(e.get("message")), cstr(e.get("details"))]))
				)
				for e in errors
			)
		return _("Unexpected response from Starshipit (HTTP {0}).").format(response.status_code)

	# --- Rates ----------------------------------------------------------------

	def get_available_services(
		self,
		pickup_address,
		delivery_address,
		parcels: list[dict],
		value_of_goods: float | None = None,
		return_order: bool = False,
		checkout: bool = False,
	) -> list[dict]:
		"""Rates for every courier service on the account, cheapest first. For checkout, a slow
		or failed answer raises StarshipitError instead of showing an alert."""
		payload = {
			"sender": rate_address(pickup_address),
			"destination": rate_address(delivery_address),
			"packages": self.packages(parcels),
			"declared_value": flt(value_of_goods),
			"include_pricing": True,
			"return_order": bool(return_order),
			"signature_required": bool(self.settings.signature_required),
		}
		if checkout:
			data = self.request(
				"POST", "/deliveryservices", payload=payload, timeout=CHECKOUT_TIMEOUT, retry=False
			)
		else:
			try:
				data = self.request("POST", "/deliveryservices", payload=payload)
			except frappe.ValidationError:
				raise
			except Exception:
				show_error_alert("fetching Starshipit rates")
				return []
		services = [self.service_dict(service) for service in data.get("services") or []]
		return sorted((s for s in services if s.total_price is not None), key=lambda s: flt(s.total_price))

	@staticmethod
	def service_dict(service: dict):
		return frappe._dict(
			service_provider=STARSHIPIT_PROVIDER,
			carrier=service.get("carrier_name") or service.get("carrier"),
			carrier_code=service.get("carrier"),
			service_name=service.get("service_name"),
			service_code=service.get("service_code"),
			service_id=SERVICE_ID_SEPARATOR.join(
				[cstr(service.get("carrier")), cstr(service.get("service_code"))]
			),
			total_price=service.get("total_price"),
		)

	def packages(self, parcels: list[dict]) -> list[dict]:
		"""ERPNext parcels (cm, kg, with a count) to Starshipit packages (m, kg, one per box)."""
		template = None
		if self.settings.default_parcel_template:
			template = frappe.get_cached_doc(
				"Shipment Parcel Template", self.settings.default_parcel_template
			)
		packages = []
		for parcel in parcels or [{}]:
			parcel = frappe._dict(parcel)
			for _box in range(max(cint(parcel.count) or 1, 1)):
				package = {
					"name": _("Parcel {0}").format(len(packages) + 1),
					"weight": flt(parcel.weight or (template and template.weight), WEIGHT_DECIMALS),
				}
				for dimension in ("length", "width", "height"):
					value = flt(parcel.get(dimension) or (template and template.get(dimension)))
					if value:
						package[dimension] = flt(value / 100, 3)
				packages.append(package)
		return packages

	# --- Orders ---------------------------------------------------------------

	def create_shipment(
		self,
		shipment: str,
		pickup_address,
		pickup_contact,
		delivery_address,
		delivery_contact,
		delivery_company_name: str | None,
		shipment_parcel,
		description_of_content: str | None,
		value_of_goods: float | None,
		service_info: dict,
	):
		"""Create (or update) the order in Starshipit and set the chosen courier. No label."""
		shipment_doc = frappe.get_doc("Shipment", shipment)
		parcels = json.loads(shipment_parcel) if isinstance(shipment_parcel, str) else shipment_parcel
		carrier_code, service_code = split_service_id(service_info)
		if shipment_doc.get("delivery_customer") and (
			frappe.get_cached_value("Customer", shipment_doc.delivery_customer, "customer_type")
			== "Individual"
		):
			delivery_company_name = None  # a person's name is not a company on the label
		order = {
			"order_number": shipment,
			"reference": cstr(
				", ".join(d.delivery_note for d in shipment_doc.shipment_delivery_note) or shipment
			)[:50],
			"shipping_method": cstr(service_info.get("service_name"))[:100],
			"signature_required": bool(self.settings.signature_required),
			"currency": shipment_currency(shipment_doc),
			"sender_details": party(pickup_address, pickup_contact, pickup_company_name(shipment_doc)),
			"destination": party(delivery_address, delivery_contact, delivery_company_name),
			"items": order_items(shipment_doc, description_of_content, value_of_goods),
			"packages": self.packages(parcels),
		}
		existing = self.find_order(shipment)
		if existing and cstr(existing.get("status")).lower() not in UNPRINTED_STATUSES:
			# Starshipit doesn't change an order once its label is printed
			chosen = tuple(cstr(code) for code in split_service_id(service_info))
			if chosen != (cstr(order_carrier(existing)), cstr(existing.get("carrier_service_code"))):
				frappe.throw(
					_(
						"The label for {0} is printed already. Cancel the Shipment to send it another way."
					).format(frappe.bold(shipment)),
					title=_("Label Printed"),
				)
			order_id = existing["order_id"]
		elif existing:
			# repeated or retried: update the order already in Starshipit, never create a second one.
			# Update Order adds rows sent without an id, so reuse the ids and delete leftover rows.
			order_id = existing["order_id"]
			order.update(
				order_id=order_id,
				carrier=carrier_code,
				carrier_service_code=service_code,
				items=sync_rows(order["items"], existing.get("items"), "item_id"),
				packages=sync_rows(order["packages"], existing.get("packages"), "package_id"),
			)
			self.request("PUT", "/orders", payload={"order": order})
		else:
			created = self.request("POST", "/orders", payload={"order": order}).get("order") or {}
			order_id = created.get("order_id")
			if not order_id:
				frappe.throw(_("Starshipit did not return an order ID."), title=_("Starshipit"))
			# Create Order has no courier fields; the courier and service are set with Update Order
			self.request(
				"PUT",
				"/orders",
				payload={
					"order": {
						"order_id": order_id,
						"carrier": carrier_code,
						"carrier_service_code": service_code,
					}
				},
			)
		return {
			"service_provider": STARSHIPIT_PROVIDER,
			"shipment_id": cstr(order_id),
			"carrier": service_info.get("carrier"),
			"carrier_service": service_info.get("service_name"),
			"shipment_amount": service_info.get("total_price"),
			"awb_number": "",
		}

	def find_order(self, order_number: str) -> dict | None:
		"""The Starshipit order with this order number, unless there is none or it was archived.
		Search Orders answers with no orders when there is none; Get Order answers with an error."""
		data = self.request(
			"GET",
			"/orders/search",
			params={"phrase": order_number, "fields": "Order_Number", "status": "All"},
		)
		found = next(
			(
				order
				for order in data.get("orders") or []
				if cstr(order.get("order_number")) == cstr(order_number) and not order.get("archived")
			),
			None,
		)
		return self.get_order(found["order_id"]) if found else None

	def get_order(self, order_id) -> dict:
		return self.request("GET", "/orders", params={"order_id": cint(order_id)}).get("order") or {}

	def cancel_order(self, order_id: str, printed: bool = False) -> str:
		"""Delete an unprinted order, or archive one whose label was printed.

		Archiving does not refund a printed label; cancel the consignment with the courier if
		it will not be sent.
		"""
		if not printed:
			try:
				self.request("DELETE", "/orders/delete", params={"order_id": order_id}, log_errors=False)
				return "deleted"
			except frappe.ValidationError:
				frappe.clear_messages()
		self.request("POST", "/orders/archive", params={"order_id": order_id})
		return "archived"

	# --- Labels and documents -------------------------------------------------

	def already_printed(self, shipment_doc, order: dict | None = None) -> bool:
		"""True when a label exists, so printing again is a free reprint."""
		if shipment_doc.awb_number:
			return True
		if order is None:
			try:
				order = self.request(
					"GET",
					"/orders",
					params={"order_id": cint(shipment_doc.shipment_id)},
					log_errors=False,
				).get("order")
			except frappe.ValidationError:
				frappe.clear_messages()
				return False
		return bool(order) and cstr(order.get("status")).lower() not in UNPRINTED_STATUSES

	def print_label(self, shipment_doc) -> tuple[list[bytes], list[str]]:
		"""Label PDFs and tracking numbers for the Starshipit order behind a Shipment."""
		# one label at a time per Shipment, so two clicks don't buy two labels
		frappe.db.get_value("Shipment", shipment_doc.name, "name", for_update=True)
		order = self.get_order(shipment_doc.shipment_id)
		reprint = self.already_printed(shipment_doc, order)
		if not reprint and not self.settings.allow_label_printing:
			frappe.throw(
				_(
					"Label printing is off for {0}. Print it in Starshipit, or enable Allow Label Printing in {1}."
				).format(
					frappe.bold(shipment_doc.name),
					get_link_to_form("Starshipit", "Starshipit", _("Starshipit Settings")),
				),
				title=_("Label Printing Off"),
			)
		payload = {"order_id": cint(shipment_doc.shipment_id), "reprint": reprint}
		if not reprint:
			carrier, service_code = order_carrier(order), order.get("carrier_service_code")
			if not carrier or not service_code:
				service = self.order_service(shipment_doc)
				carrier, service_code = service.carrier_code, service.service_code
			payload.update(carrier=carrier, carrier_service_code=service_code)
		data = self.request("POST", "/orders/shipment", payload=payload)
		labels = [base64.b64decode(label) for label in data.get("labels") or []]
		return labels, [number for number in data.get("tracking_numbers") or [] if number]

	def order_service(self, shipment_doc):
		"""The service chosen for this Shipment (matched by name) among the order's services."""
		data = self.request(
			"POST",
			"/deliveryservices",
			payload={"order_id": cint(shipment_doc.shipment_id), "include_pricing": False},
		)
		services = [self.service_dict(service) for service in data.get("services") or []]
		chosen = [s for s in services if s.service_name == shipment_doc.carrier_service]
		if not chosen and len(services) == 1:
			chosen = services
		if not chosen:
			frappe.throw(
				_("{0} is not available for this order.").format(
					frappe.bold(shipment_doc.carrier_service or "?")
				),
				title=_("Starshipit"),
			)
		return chosen[0]

	def commercial_invoices(self, shipment_doc) -> list[bytes]:
		data = self.request(
			"POST", "/orders/commercialinvoices", payload={"order_ids": [cint(shipment_doc.shipment_id)]}
		)
		return [
			base64.b64decode(invoice["invoice_base64_string"])
			for invoice in data.get("invoiceList") or []
			if invoice.get("invoice_base64_string")
		]

	def manifest(self, shipment_doc) -> bytes | None:
		"""Manifest the printed order (sends it to the courier for billing). Returns the manifest PDF."""
		order = self.get_order(shipment_doc.shipment_id)
		if not self.already_printed(shipment_doc, order):
			frappe.throw(_("Print the label before manifesting."), title=_("Starshipit"))
		if order.get("manifested"):
			frappe.throw(_("This order is already manifested in Starshipit."), title=_("Starshipit"))
		data = self.request(
			"POST", "/orders/manifest", payload={"order_ids": [cint(shipment_doc.shipment_id)]}
		)
		return base64.b64decode(data["pdf"]) if data.get("pdf") else None

	def book_pickup(self, shipment_doc, special_instructions: str | None = None) -> str:
		"""Book a courier pickup for the Shipment's pickup date and time window. Returns the job number."""
		if not shipment_doc.pickup_date or not shipment_doc.pickup_from or not shipment_doc.pickup_to:
			frappe.throw(_("Set the Pickup Date, Pickup From and Pickup To first."), title=_("Starshipit"))
		order = self.get_order(shipment_doc.shipment_id)
		if cstr(order.get("status")).lower() in UNPRINTED_STATUSES:
			frappe.throw(_("Print the label before booking a pickup."), title=_("Starshipit"))

		zone = frappe.get_system_settings("time_zone") or "UTC"
		packages = order.get("packages") or []
		payload = {
			"order_reference": shipment_doc.name,
			"tracking_number": cstr(shipment_doc.awb_number).split(",")[0].strip(),
			"carrier_name": order_carrier(order) or order.get("carrier_name") or shipment_doc.carrier,
			"service_code": order.get("carrier_service_code"),
			"pickup_timeslot_start": local_to_utc(shipment_doc.pickup_date, shipment_doc.pickup_from, zone),
			"pickup_timeslot_end": local_to_utc(shipment_doc.pickup_date, shipment_doc.pickup_to, zone),
			"special_instructions": cstr(special_instructions)[:255],
			"collection_address": pickup_address_model(order.get("sender_details") or {}),
			"receiver_address": pickup_address_model(order.get("destination") or {}),
			"consignment_count": 1,
			"package_count": len(packages) or 1,
			"total_weight": flt(sum(flt(p.get("weight")) for p in packages), WEIGHT_DECIMALS),
			"max_package_weight": max((flt(p.get("weight")) for p in packages), default=0),
			# packages are in metres; pickup dimensions are in centimetres
			"max_package_length": max((flt(p.get("length")) * 100 for p in packages), default=0),
			"max_package_width": max((flt(p.get("width")) * 100 for p in packages), default=0),
			"max_package_height": max((flt(p.get("height")) * 100 for p in packages), default=0),
			"shipment_ids": [cint(shipment_doc.shipment_id)],
		}
		if WINDOWS_TIMEZONES.get(zone):
			payload["pickup_timezone_id"] = WINDOWS_TIMEZONES[zone]
		data = self.request("POST", "/pickup/book", payload=payload)
		return cstr(data.get("job_number"))

	# --- Status ---------------------------------------------------------------

	def get_tracking_data(self, order_number: str) -> dict | None:
		"""Status of the Starshipit order: courier tracking when there is any, else the order status."""
		order = self.find_order(order_number)
		if not order:
			return None
		tracking = {}
		try:
			response = self.request("GET", "/track", params={"order_number": order_number}, log_errors=False)
			tracking = tracking_result(response.get("results"), order_number)
		except frappe.ValidationError:
			frappe.clear_messages()
		return status_dict(order, tracking)


# --- Helpers ------------------------------------------------------------------


def wait_for_turn() -> None:
	"""Keep to the API's request rate across every worker on the site."""
	last = frappe.utils.flt(frappe.cache.get_value(PACING_KEY))
	wait = MIN_REQUEST_INTERVAL - (time.time() - last)
	if wait > 0:
		time.sleep(wait)
	frappe.cache.set_value(PACING_KEY, time.time(), expires_in_sec=5)


def sync_rows(rows: list[dict], existing: list[dict] | None, id_field: str) -> list[dict]:
	"""Rows for Update Order that replace the order's rows: reuse ids in order, delete the rest."""
	existing = [row for row in existing or [] if row.get(id_field)]
	synced = [{**row, id_field: old[id_field]} for row, old in zip(rows, existing)]
	synced += [dict(row) for row in rows[len(existing) :]]
	synced += [{id_field: old[id_field], "delete": True} for old in existing[len(rows) :]]
	return synced


def order_carrier(order: dict) -> str | None:
	"""The order's carrier code, or None while it is unset (Starshipit returns -1 then)."""
	carrier = order.get("carrier")
	return None if carrier in (None, "", -1, "-1") else cstr(carrier)


def tracking_result(results, order_number: str) -> dict:
	"""The Tracking Details for the order, whether /track returned one object or a list."""
	if isinstance(results, dict):
		return results
	results = [r for r in results or [] if isinstance(r, dict)]
	matching = [r for r in results if cstr(r.get("order_number")) == cstr(order_number)]
	return (matching or results or [{}])[-1]


def split_service_id(service_info: dict) -> tuple[str, str]:
	carrier_code, _sep, service_code = cstr(service_info.get("service_id")).partition(SERVICE_ID_SEPARATOR)
	return carrier_code or service_info.get("carrier_code"), service_code or service_info.get("service_code")


def pickup_company_name(shipment_doc) -> str | None:
	if shipment_doc.get("pickup_from_type") == "Company" and shipment_doc.get("pickup_company"):
		return frappe.get_cached_value("Company", shipment_doc.pickup_company, "company_name")
	return None


def shipment_currency(shipment_doc) -> str | None:
	company = (
		shipment_doc.get("pickup_company")
		or shipment_doc.get("delivery_company")
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	return frappe.get_cached_value("Company", company, "default_currency") if company else None


def rate_address(address) -> dict:
	return {
		"street": address.address_line1,
		"suburb": address.address_line2 or "",
		"city": address.city,
		"state": address.get("state") or "",
		"post_code": address.pincode,
		"country_code": address.country_code.upper(),
	}


def party(address, contact, company_name: str | None = None) -> dict:
	"""Sender Details / Destination Details model."""
	contact = contact or frappe._dict()
	name = " ".join(filter(None, [contact.get("first_name"), contact.get("last_name")]))
	return {
		"name": cstr(name or company_name or address.address_title)[:100],
		"email": cstr(contact.get("email_id"))[:100],
		"phone": cstr(contact.get("phone") or contact.get("mobile_no"))[:100],
		"company": cstr(company_name)[:100],
		"street": cstr(address.address_line1)[:100],
		"suburb": cstr(address.address_line2)[:100],
		"city": cstr(address.city)[:100],
		"state": cstr(address.get("state"))[:100],
		"post_code": cstr(address.pincode)[:10],
		"country": address.country,
	}


def pickup_address_model(details: dict) -> dict:
	"""A Starshipit order's sender or destination as a Pickup Address Model."""
	return {
		"contact_name": details.get("name") or "",
		"contact_email": details.get("email") or "",
		"contact_phone": details.get("phone") or "",
		"company_name": details.get("company") or "",
		"address_line1": details.get("street") or "",
		"address_line2": details.get("building") or "",
		"suburb": details.get("suburb") or details.get("city") or "",
		"postcode": details.get("post_code") or "",
		"state": details.get("state") or "",
		"country": details.get("country") or "",
	}


def local_to_utc(date, time_value, zone: str) -> str:
	local = datetime.combine(getdate(date), get_time(time_value)).replace(tzinfo=ZoneInfo(zone))
	return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def kg_per_unit(weight: float, uom: str | None) -> float | None:
	"""Weight in kg, or None when the unit is not a known mass unit."""
	unit = cstr(uom).strip().lower()
	if not weight:
		return None
	if unit in ("kg", "kgs", "kilogram", "kilograms"):
		return flt(weight)
	if unit in ("g", "gm", "gram", "grams"):
		return flt(weight) / 1000
	if uom:
		factor = frappe.db.get_value("UOM Conversion Factor", {"from_uom": uom, "to_uom": "Kg"}, "value")
		if factor:
			return flt(weight) * flt(factor)
	return None


def order_items(shipment_doc, description_of_content: str | None, value_of_goods: float | None) -> list[dict]:
	"""Create Order - Item Model rows from the Shipment's delivery notes, customs data from the Item."""
	items = []
	for row in shipment_doc.shipment_delivery_note:
		for item in frappe.get_all(
			"Delivery Note Item",
			filters={"parent": row.delivery_note},
			fields=["item_code", "item_name", "stock_qty", "base_amount", "weight_per_unit", "weight_uom"],
			order_by="idx",
		):
			tariff_code, origin, is_stock = frappe.get_cached_value(
				"Item", item.item_code, ["customs_tariff_number", "country_of_origin", "is_stock_item"]
			)
			if not is_stock and not item.weight_per_unit:
				continue  # shipping, fees and other services are not goods in the parcel
			line = {
				"description": item.item_name,
				"sku": cstr(item.item_code)[:50],
				# per stock unit, like weight_per_unit; value in the company currency sent as "currency"
				"quantity": flt(item.stock_qty),
				"value": flt(flt(item.base_amount) / flt(item.stock_qty), 2) if item.stock_qty else 0,
			}
			weight = kg_per_unit(item.weight_per_unit, item.weight_uom)
			if weight:
				line["weight"] = flt(weight, WEIGHT_DECIMALS)
			if tariff_code:
				line["tariff_code"] = cstr(tariff_code)[:18]
			if origin:
				line["country_of_origin"] = cstr(origin)[:50]
			items.append(line)
	if not items:
		items.append(
			{
				"description": description_of_content or shipment_doc.name,
				"sku": cstr(shipment_doc.name)[:50],
				"quantity": 1,
				"value": flt(value_of_goods),
			}
		)
	return items


def normalize_status(status: str | None) -> str:
	"""'InTransit' -> 'In Transit', 'OutForDelivery' -> 'Out For Delivery'; 'Delivered' stays."""
	return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cstr(status)).strip()


def erpnext_tracking_status(status: str | None) -> str:
	"""Map a Starshipit status to the Shipment's tracking_status options."""
	if cstr(status).replace(" ", "").lower() in NOT_IN_TRANSIT_STATUSES:
		return ""  # no label yet, or cancelled: nothing is in transit
	return "Delivered" if cstr(status).replace(" ", "").lower() in DELIVERED_STATUSES else "In Progress"


def status_dict(order: dict, tracking: dict | None = None) -> dict:
	"""Shipment tracking fields from a Starshipit order (Full Order Model) and its tracking."""
	tracking = tracking or {}
	package_numbers = [
		p.get("tracking_number") for p in order.get("packages") or [] if p.get("tracking_number")
	]
	tracking_number = tracking.get("tracking_number") or ", ".join(package_numbers)
	status = tracking.get("tracking_status") or order.get("status") or ""
	if order.get("manifested") and cstr(status).lower() in ("printed", "saved"):
		status = "Manifested"
	events = tracking.get("tracking_events") or []
	detail = events[-1].get("details") if events else ""
	info = normalize_status(status)
	if detail and detail.lower() != info.lower():
		info = f"{info}: {detail}"
	return {
		"awb_number": tracking_number or "",
		"tracking_status": erpnext_tracking_status(status),
		"tracking_status_info": cstr(info)[:140],
		"tracking_url": tracking.get("tracking_url") or package_tracking_url(order),
	}


def package_tracking_url(order: dict) -> str:
	for package in order.get("packages") or []:
		url = cstr(package.get("branded_tracking_url") or package.get("tracking_url"))
		if url.startswith("http"):
			return url
	return ""


def attach_pdf(shipment: str, prefix: str, content: bytes, index: int | None = None) -> str:
	"""Attach a PDF to the Shipment, reusing an existing file of the same name."""
	file_name = f"{prefix}_{shipment}{f'_{index}' if index else ''}.pdf"
	existing = frappe.db.get_value(
		"File",
		{"attached_to_doctype": "Shipment", "attached_to_name": shipment, "file_name": file_name},
		"file_url",
	)
	if existing:
		return existing
	attachment = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"content": content,
			"attached_to_doctype": "Shipment",
			"attached_to_name": shipment,
			"is_private": 1,
		}
	)
	attachment.insert(ignore_permissions=True)
	return attachment.file_url


def apply_status(shipment_name: str, data: dict):
	"""Write Starshipit status to the Shipment and its Delivery Notes."""
	from erpnext_shipping.erpnext_shipping.shipping import update_delivery_note

	values = {k: data[k] for k in ("tracking_status", "tracking_status_info") if data.get(k) is not None}
	# a delivered parcel stays delivered, whatever order late or repeated events arrive in
	if frappe.db.get_value("Shipment", shipment_name, "tracking_status") == "Delivered" and data.get(
		"tracking_status"
	) not in ("Delivered", "Returned"):
		values.pop("tracking_status", None)
		values.pop("tracking_status_info", None)
	for field in ("awb_number", "tracking_url"):
		if data.get(field):
			values[field] = data[field]
	if data.get("tracking_status") == "Delivered":
		values["status"] = "Completed"
	frappe.db.set_value("Shipment", shipment_name, values)
	delivery_notes = frappe.get_all(
		"Shipment Delivery Note", filters={"parent": shipment_name}, pluck="delivery_note"
	)
	if delivery_notes:
		# the Delivery Notes show what the Shipment now has, never an older status
		stored = frappe.db.get_value(
			"Shipment",
			shipment_name,
			["awb_number", "tracking_url", "tracking_status", "tracking_status_info"],
			as_dict=True,
		)
		update_delivery_note(delivery_notes=delivery_notes, tracking_info=stored)


# --- Shipment actions (form buttons) ------------------------------------------


def get_starshipit_shipment(shipment: str):
	doc = frappe.get_doc("Shipment", shipment)
	doc.check_permission("write")
	if doc.service_provider != STARSHIPIT_PROVIDER or not doc.shipment_id:
		frappe.throw(_("This Shipment was not created with Starshipit."))
	return doc


def refresh_status(shipment: str) -> dict | None:
	data = StarshipitUtils().get_tracking_data(shipment)
	if data:
		apply_status(shipment, data)
	return data


@frappe.whitelist()
def manifest_shipment(shipment: str) -> str | None:
	doc = get_starshipit_shipment(shipment)
	pdf = StarshipitUtils().manifest(doc)
	doc.add_comment("Info", _("Manifested in Starshipit."))
	refresh_status(doc.name)
	return attach_pdf(doc.name, "manifest", pdf) if pdf else None


@frappe.whitelist()
def book_pickup(
	shipment: str,
	pickup_date: str | None = None,
	pickup_from: str | None = None,
	pickup_to: str | None = None,
	special_instructions: str | None = None,
) -> str:
	doc = get_starshipit_shipment(shipment)
	if pickup_date or pickup_from or pickup_to:
		# pickup fields can be changed after submit; saving runs ERPNext's pickup time checks
		doc.pickup_date = pickup_date or doc.pickup_date
		doc.pickup_from = pickup_from or doc.pickup_from
		doc.pickup_to = pickup_to or doc.pickup_to
		doc.save()
	job_number = StarshipitUtils().book_pickup(doc, special_instructions)
	doc.add_comment(
		"Info",
		_("Pickup booked for {0}, {1} to {2}. Job number: {3}").format(
			frappe.format(doc.pickup_date, {"fieldtype": "Date"}),
			cstr(doc.pickup_from)[:5],
			cstr(doc.pickup_to)[:5],
			job_number or "-",
		),
	)
	return job_number


@frappe.whitelist()
def create_commercial_invoice(shipment: str) -> list[str]:
	doc = get_starshipit_shipment(shipment)
	invoices = StarshipitUtils().commercial_invoices(doc)
	if not invoices:
		frappe.throw(_("Starshipit returned no commercial invoice for this order."), title=_("Starshipit"))
	return [attach_pdf(doc.name, "commercial_invoice", content, i) for i, content in enumerate(invoices, 1)]


# --- Events -------------------------------------------------------------------


def cancel_shipment_order(doc, method=None):
	"""Shipment on_cancel: remove the order from Starshipit."""
	if doc.service_provider != STARSHIPIT_PROVIDER or not doc.shipment_id:
		return
	if not frappe.db.get_single_value("Starshipit", "enabled"):
		return
	# a failure stops the cancel, so the Shipment is never cancelled while its order is still live
	utils = StarshipitUtils()
	result = utils.cancel_order(doc.shipment_id, printed=utils.already_printed(doc))
	if result == "archived":
		frappe.msgprint(
			_("Starshipit order archived. Cancel the consignment with the courier if it won't be sent."),
			indicator="orange",
			alert=True,
		)


def valid_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
	"""HMAC-SHA256 of the raw body with the webhook secret, as Starshipit signs it."""
	if not signature:
		return False
	expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected.lower(), cstr(signature).strip().lower())


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(token: str | None = None):
	"""Tracking update pushed by Starshipit.

	Authenticated by the X-Starshipit-Signature header when a webhook secret is set, otherwise
	by the token in the webhook URL. Repeated or late events don't undo a delivery.
	"""
	settings = frappe.get_single("Starshipit")
	secret = settings.get_password("webhook_secret", raise_exception=False)
	raw_body = frappe.request.get_data() if frappe.request else b""
	if secret:
		if not valid_signature(raw_body, frappe.get_request_header(SIGNATURE_HEADER), secret):
			raise frappe.PermissionError
	else:
		# Starshipit posts JSON, so Frappe takes the arguments from the body; the token is in the URL
		token = token or (frappe.request.args.get("token") if frappe.request else None)
		expected = settings.webhook_token
		if not expected or not token or not secrets.compare_digest(cstr(token), cstr(expected)):
			raise frappe.PermissionError

	try:
		payload = json.loads(raw_body or b"{}")
	except ValueError:
		payload = None
	if not isinstance(payload, dict):
		frappe.local.response.http_status_code = 400
		return {"ok": False}

	order_number = cstr(payload.get("order_number"))
	shipment = frappe.db.get_value(
		"Shipment",
		{"name": order_number, "service_provider": STARSHIPIT_PROVIDER, "docstatus": 1},
		"name",
	)
	if not shipment:
		frappe.log_error(
			title="Starshipit webhook: no matching Shipment",
			message=json.dumps(payload, indent=1, default=str),
		)
		return {"ok": True, "matched": False}

	status = payload.get("tracking_status")
	data = {
		"awb_number": cstr(payload.get("tracking_number")),
		"tracking_status": erpnext_tracking_status(status),
		"tracking_status_info": normalize_status(status),
	}
	apply_status(shipment, data)
	if not frappe.db.get_value("Shipment", shipment, "tracking_url"):
		# the webhook carries no tracking URL; fetch it after answering, as Starshipit recommends
		frappe.enqueue(
			"erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit.refresh_status",
			shipment=shipment,
			enqueue_after_commit=True,
		)
	return {"ok": True, "matched": True}
