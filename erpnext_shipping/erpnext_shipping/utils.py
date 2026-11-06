# Copyright (c) 2020, Frappe Technologies and contributors
# For license information, please see license.txt
import re

import frappe
from frappe import _
from frappe.utils import flt
from frappe.utils.data import get_link_to_form


def get_tracking_url(carrier, tracking_number):
	# Return the formatted Tracking URL.
	tracking_url = ""
	url_reference = frappe.db.get_value("Parcel Service", carrier, "url_reference")
	if url_reference:
		tracking_url = frappe.render_template(url_reference, {"tracking_number": tracking_number})
	return tracking_url


def get_address(address_name):
	address = frappe.db.get_value(
		"Address",
		address_name,
		[
			"address_title",
			"address_line1",
			"address_line2",
			"city",
			"state",
			"pincode",
			"country",
		],
		as_dict=1,
	)
	validate_address(address)

	address.country = address.country.strip()
	address.country_code = get_country_code(address.country)
	address.pincode = address.pincode.replace(" ", "")
	address.city = address.city.strip()

	return address


def validate_address(address):
	if not address.country:
		frappe.throw(f"Please add a valid country in Address {address.address_title}.")

	if not address.pincode or address.pincode.strip() == "":
		frappe.throw(_("Please add a valid pincode in Address {0}.").format(address.address_title))


def validate_phone(doc, method=None):
	if doc.pickup_from_type == "Company":
		phone_number = frappe.db.get_value("User", doc.pickup_contact_person, "phone")
	else:
		phone_number = frappe.db.get_value("Contact", doc.pickup_contact_name, "phone")

	if not phone_number:
		frappe.throw(_("Pickup contact phone is required."))

	if not re.match(r"^\+(?![\s0])[\d\s]+\d$", phone_number):
		frappe.throw(_("Pickup contact phone must consist of a '+' followed by one or more digits."))


def get_country_code(country_name):
	country_code = frappe.db.get_value("Country", country_name, "code")
	if not country_code:
		frappe.throw(_("Country Code not found for {0}").format(country_name))
	return country_code


def get_contact(contact_name):
	fields = ["first_name", "last_name", "email_id", "phone", "mobile_no", "gender"]
	contact = frappe.db.get_value("Contact", contact_name, fields, as_dict=1)

	if not contact.last_name:
		frappe.throw(
			msg=_("Please set Last Name for Contact {0}").format(get_link_to_form("Contact", contact_name)),
			title=_("Last Name is mandatory to continue."),
		)

	if not contact.phone:
		contact.phone = contact.mobile_no

	return contact


def match_parcel_service_type_carrier(
	shipment_prices: list[dict], carrier_fieldname: str, service_fieldname: str
):
	from erpnext_shipping.erpnext_shipping.doctype.parcel_service_type.parcel_service_type import (
		match_parcel_service_type_alias,
	)

	for idx, prices in enumerate(shipment_prices):
		service_name = match_parcel_service_type_alias(
			prices.get(carrier_fieldname), prices.get(service_fieldname)
		)
		is_preferred = frappe.db.get_value(
			"Parcel Service Type", service_name, "show_in_preferred_services_list"
		)
		if is_preferred:
			shipment_prices[idx].is_preferred = is_preferred

	return shipment_prices


def show_error_alert(action):
	log = frappe.log_error(title="Shipping Error")
	link_to_log = get_link_to_form("Error Log", log.name, "See what happened.")
	frappe.msgprint(
		msg=_("An Error occurred while {0}. {1}").format(action, link_to_log), indicator="orange", alert=True
	)


def update_tracking_info_daily():
	"""Daily scheduled event to update Tracking info for not delivered Shipments

	Also Updates the related Delivery Notes.
	"""
	from erpnext_shipping.erpnext_shipping.shipping import update_tracking

	shipments = frappe.get_all(
		"Shipment",
		filters={
			"docstatus": 1,
			"status": "Booked",
			"shipment_id": ["!=", ""],
			"tracking_status": ["!=", "Delivered"],
		},
	)
	for shipment in shipments:
		shipment_doc = frappe.get_doc("Shipment", shipment.name)
		tracking_info = update_tracking(
			shipment.name,
			shipment_doc.service_provider,
			shipment_doc.shipment_id,
			shipment_doc.shipment_delivery_note,
		)

		if tracking_info:
			fields = ["awb_number", "tracking_status", "tracking_status_info", "tracking_url"]
			for field in fields:
				shipment_doc.db_set(field, tracking_info.get(field))


def fill_parcels_from_items(doc, method=None):
	"""Shipment before_validate: when a draft Shipment has no parcels, add one from its items.

	Weight = sum of item weight x quantity from the Delivery Notes, plus the box from the Parcel
	Template (the Shipment's, else Starshipit's default). Size comes from the template. Nothing is
	filled when no item has a weight; parcels can always be edited or split by hand.
	"""
	from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import kg_per_unit

	if doc.docstatus != 0 or doc.shipment_parcel or not doc.shipment_delivery_note:
		return
	items_weight = 0.0
	for row in doc.shipment_delivery_note:
		for item in frappe.get_all(
			"Delivery Note Item",
			filters={"parent": row.delivery_note},
			fields=["qty", "weight_per_unit", "weight_uom"],
		):
			items_weight += (kg_per_unit(item.weight_per_unit, item.weight_uom) or 0) * flt(item.qty)
	if not items_weight:
		return
	template_name = doc.parcel_template or frappe.db.get_single_value("Starshipit", "default_parcel_template")
	template = frappe.get_cached_doc("Shipment Parcel Template", template_name) if template_name else None
	doc.append(
		"shipment_parcel",
		{
			"length": template.length if template else 0,
			"width": template.width if template else 0,
			"height": template.height if template else 0,
			"weight": flt(items_weight + flt(template.weight if template else 0), 3),
			"count": 1,
		},
	)
