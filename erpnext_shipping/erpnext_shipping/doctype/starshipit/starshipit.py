# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Starshipit integration.

Starshipit connects to many couriers (NZ Post, CourierPost, Aramex, Australia Post,
DHL, ...). This provider quotes every service on the Starshipit account, creates
the order in Starshipit, optionally prints the label, and keeps tracking up to date
through the tracking endpoint and Starshipit's webhook.

Creating an order in Starshipit is free. Printing a label books the consignment
with the courier and is charged, so it is refused unless "Allow Label Printing" is
switched on; reprinting an existing label is always allowed.

API reference: https://api-docs.starshipit.com/
"""

import base64
import json
import re
import secrets

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt, get_url
from frappe.utils.data import get_link_to_form

from erpnext_shipping.erpnext_shipping.utils import show_error_alert

STARSHIPIT_PROVIDER = "Starshipit"
BASE_URL = "https://api.starshipit.com/api"
TIMEOUT = 30
WEIGHT_DECIMALS = 3
SERVICE_ID_SEPARATOR = "|"
WEBHOOK_METHOD = "erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit.webhook"


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

	def request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
		if not self.api_key or not self.subscription_key:
			frappe.throw(_("Set the Starshipit API Key and Subscription Key first."), title=_("Starshipit"))
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
			timeout=TIMEOUT,
		)
		try:
			data = response.json()
		except ValueError:
			data = None
		if response.status_code in (401, 403):
			frappe.throw(_("Starshipit rejected the API keys."), title=_("Starshipit"))
		if not response.ok or (isinstance(data, dict) and data.get("success") is False):
			frappe.throw(self.format_errors(data, response), title=_("Starshipit"))
		return data or {}

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
	) -> list[dict]:
		"""Rates for every courier service on the account, cheapest first."""
		payload = {
			"sender": rate_address(pickup_address),
			"destination": rate_address(delivery_address),
			"packages": self.packages(parcels),
			"declared_value": flt(value_of_goods),
			"include_pricing": True,
			"return_order": bool(return_order),
			"signature_required": bool(self.settings.signature_required),
		}
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
			for _box in range(max(int(parcel.count or 1), 1)):
				package = {"weight": flt(parcel.weight or (template and template.weight), WEIGHT_DECIMALS)}
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
		"""Create the order in Starshipit with the chosen service. No label is printed."""
		shipment_doc = frappe.get_doc("Shipment", shipment)
		parcels = json.loads(shipment_parcel) if isinstance(shipment_parcel, str) else shipment_parcel
		carrier_code, service_code = split_service_id(service_info)
		currency = frappe.get_cached_value("Company", shipment_doc.company, "default_currency")
		order = {
			"order_number": shipment,
			"reference": ", ".join(d.delivery_note for d in shipment_doc.shipment_delivery_note) or shipment,
			"carrier": carrier_code,
			"carrier_service_code": service_code,
			"shipping_method": service_info.get("service_name"),
			"signature_required": bool(self.settings.signature_required),
			"currency": currency,
			"declared_value": flt(value_of_goods),
			"sender_details": party(pickup_address, pickup_contact),
			"destination": party(delivery_address, delivery_contact, delivery_company_name),
			"items": order_items(shipment_doc, description_of_content, value_of_goods),
			"packages": self.packages(parcels),
		}
		data = self.request("POST", "/orders", payload={"order": order})
		created = data.get("order") or {}
		if not created.get("order_id"):
			frappe.throw(_("Starshipit did not return an order ID."), title=_("Starshipit"))
		return {
			"service_provider": STARSHIPIT_PROVIDER,
			"shipment_id": cstr(created["order_id"]),
			"carrier": service_info.get("carrier") or created.get("carrier_name"),
			"carrier_service": service_info.get("service_name"),
			"shipment_amount": service_info.get("total_price"),
			"awb_number": "",
		}

	def cancel_order(self, order_id: str, printed: bool = False) -> str:
		"""Delete an unshipped order, or archive one whose label was printed.

		A printed label's courier charge is not refunded by archiving; cancel the
		consignment with the courier if needed.
		"""
		if not printed:
			try:
				self.request("DELETE", "/orders/delete", params={"order_id": order_id})
				return "deleted"
			except frappe.ValidationError:
				frappe.clear_messages()
		self.request("POST", "/orders/archive", params={"order_id": order_id})
		return "archived"

	# --- Labels ---------------------------------------------------------------

	def print_label(self, shipment_doc) -> tuple[list[bytes], list[str]]:
		"""Label PDFs and tracking numbers for the Starshipit order behind a Shipment."""
		reprint = bool(shipment_doc.awb_number)
		if not reprint and not self.settings.allow_label_printing:
			frappe.throw(
				_(
					"Printing labels from ERPNext is switched off because it books the consignment and is "
					"charged. Print the label in Starshipit (order {0}), or enable Allow Label Printing in {1}."
				).format(
					frappe.bold(shipment_doc.shipment_id),
					get_link_to_form("Starshipit", "Starshipit", _("Starshipit Settings")),
				),
				title=_("Label Printing Disabled"),
			)
		data = self.request(
			"POST",
			"/orders/shipment",
			payload={"order_id": int(shipment_doc.shipment_id), "reprint": reprint},
		)
		labels = [base64.b64decode(label) for label in data.get("labels") or []]
		return labels, data.get("tracking_numbers") or []

	# --- Tracking -------------------------------------------------------------

	def get_tracking_data(self, order_number: str) -> dict | None:
		"""Latest tracking for the order, or None while no label has been printed."""
		try:
			data = self.request("GET", "/track", params={"order_number": order_number})
		except frappe.ValidationError:
			frappe.clear_messages()
			return None
		return tracking_dict(data.get("results") or {})


# --- Helpers ------------------------------------------------------------------


def split_service_id(service_info: dict) -> tuple[str, str]:
	carrier_code, _sep, service_code = cstr(service_info.get("service_id")).partition(SERVICE_ID_SEPARATOR)
	return carrier_code or service_info.get("carrier_code"), service_code or service_info.get("service_code")


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
	contact = contact or frappe._dict()
	name = " ".join(filter(None, [contact.get("first_name"), contact.get("last_name")]))
	return {
		"name": name or company_name or address.address_title,
		"email": contact.get("email_id") or "",
		"phone": contact.get("phone") or contact.get("mobile_no") or "",
		"company": company_name or "",
		"street": address.address_line1,
		"suburb": address.address_line2 or "",
		"city": address.city,
		"state": address.get("state") or "",
		"post_code": address.pincode,
		"country": address.country,
	}


def order_items(shipment_doc, description_of_content: str | None, value_of_goods: float | None) -> list[dict]:
	"""Items from the Shipment's delivery notes, with customs data from the Item master."""
	items = []
	for row in shipment_doc.shipment_delivery_note:
		for item in frappe.get_all(
			"Delivery Note Item",
			filters={"parent": row.delivery_note},
			fields=["item_code", "item_name", "qty", "rate", "weight_per_unit", "weight_uom"],
			order_by="idx",
		):
			tariff_code, origin = frappe.get_cached_value(
				"Item", item.item_code, ["customs_tariff_number", "country_of_origin"]
			)
			line = {
				"description": item.item_name,
				"sku": item.item_code,
				"quantity": flt(item.qty),
				"value": flt(item.rate),
			}
			if item.weight_per_unit and cstr(item.weight_uom).lower() in ("kg", "kilogram", "kilograms"):
				line["weight"] = flt(item.weight_per_unit, WEIGHT_DECIMALS)
			if tariff_code:
				line["tariff_code"] = tariff_code
			if origin:
				line["country_of_origin"] = origin
			items.append(line)
	if not items:
		items.append(
			{
				"description": description_of_content or shipment_doc.name,
				"sku": shipment_doc.name,
				"quantity": 1,
				"value": flt(value_of_goods),
			}
		)
	return items


def tracking_dict(results: dict) -> dict | None:
	if not results or not (results.get("tracking_number") or results.get("tracking_status")):
		return None
	events = results.get("tracking_events") or []
	latest = events[-1] if events else {}
	return {
		"awb_number": results.get("tracking_number") or "",
		"tracking_status": normalize_status(results.get("tracking_status")),
		"tracking_status_info": latest.get("details") or results.get("order_status") or "",
		"tracking_url": results.get("tracking_url") or "",
	}


def normalize_status(status: str | None) -> str:
	"""'InTransit' -> 'In Transit', 'OutForDelivery' -> 'Out For Delivery'; 'Delivered' stays."""
	return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cstr(status)).strip()


# --- Events -------------------------------------------------------------------


def cancel_shipment_order(doc, method=None):
	"""Shipment on_cancel: remove the order from Starshipit."""
	if doc.service_provider != STARSHIPIT_PROVIDER or not doc.shipment_id:
		return
	if not frappe.db.get_single_value("Starshipit", "enabled"):
		return
	try:
		result = StarshipitUtils().cancel_order(doc.shipment_id, printed=bool(doc.awb_number))
	except Exception:
		show_error_alert("cancelling the Starshipit order")
		return
	if result == "archived":
		frappe.msgprint(
			_(
				"The Starshipit order was archived. A printed label may still be charged by the courier; "
				"cancel the consignment with the courier if it will not be sent."
			),
			indicator="orange",
			alert=True,
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(token: str | None = None):
	"""Tracking update pushed by Starshipit. Authenticated by the token in the webhook URL."""
	expected = frappe.db.get_single_value("Starshipit", "webhook_token")
	if not expected or not token or not secrets.compare_digest(cstr(token), cstr(expected)):
		raise frappe.PermissionError

	payload = frappe.request.get_json(silent=True) or {}
	order_number = cstr(payload.get("order_number"))
	shipment = frappe.db.get_value(
		"Shipment",
		{"name": order_number, "service_provider": STARSHIPIT_PROVIDER, "docstatus": 1},
		["name", "tracking_url"],
		as_dict=True,
	)
	if not shipment:
		return {"ok": True, "matched": False}

	tracking = {
		"awb_number": cstr(payload.get("tracking_number")),
		"tracking_status": normalize_status(payload.get("tracking_status")),
		"tracking_status_info": cstr(payload.get("tracking_status")),
		"tracking_url": shipment.tracking_url or "",
	}
	if not tracking["tracking_url"]:
		try:
			tracking.update(StarshipitUtils().get_tracking_data(order_number) or {})
		except Exception:
			frappe.log_error(title="Starshipit webhook: tracking lookup failed")

	from erpnext_shipping.erpnext_shipping.shipping import update_delivery_note

	frappe.db.set_value("Shipment", shipment.name, tracking)
	delivery_notes = frappe.get_all(
		"Shipment Delivery Note", filters={"parent": shipment.name}, pluck="delivery_note"
	)
	if delivery_notes:
		update_delivery_note(delivery_notes=delivery_notes, tracking_info=tracking)
	return {"ok": True, "matched": True}
