import json
import frappe
from frappe.utils import nowdate

def ensure(doctype, name, values):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.insert(ignore_permissions=True)
	return doc

def setup():
	company = "Modbee"
	frappe.db.set_value("User", "Administrator", {"phone": "+64 21 000 0000", "last_name": "Test"})
	sender = ensure("Address", "API Test Sender-Shipping", {"address_title": "API Test Sender", "address_type": "Shipping",
		"address_line1": "1 Queen Street", "address_line2": "Auckland Central", "city": "Auckland", "pincode": "1010",
		"country": "New Zealand", "is_your_company_address": 1, "links": [{"link_doctype": "Company", "link_name": company}]})
	item = ensure("Item", "API-TEST-SKU", {"item_code": "API-TEST-SKU", "item_name": "API test controller", "item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "All Item Groups",
		"stock_uom": "Nos", "is_stock_item": 0, "customs_tariff_number": None, "country_of_origin": "New Zealand", "weight_per_unit": 0.5, "weight_uom": "Kg"})
	customer = ensure("Customer", "API Test Customer", {"customer_name": "API Test Customer", "customer_type": "Company"})
	addr = ensure("Address", "API Test Customer-Shipping", {"address_title": "API Test Customer", "address_type": "Shipping",
		"address_line1": "5 High Street", "city": "Dunedin", "pincode": "9016", "country": "New Zealand",
		"links": [{"link_doctype": "Customer", "link_name": customer.name}]})
	contact = frappe.db.get_value("Contact", {"email_id": "api-test@modbee.org"})
	if not contact:
		c = frappe.get_doc({"doctype": "Contact", "first_name": "API Test", "last_name": "Recipient",
			"email_ids": [{"email_id": "api-test@modbee.org", "is_primary": 1}], "phone_nos": [{"phone": "+64 3 000 0000", "is_primary_phone": 1}],
			"links": [{"link_doctype": "Customer", "link_name": customer.name}]}); c.insert(ignore_permissions=True); contact = c.name
	dn = frappe.get_doc({"doctype": "Delivery Note", "company": company, "customer": customer.name, "posting_date": nowdate(),
		"shipping_address_name": addr.name, "contact_person": contact,
		"items": [{"item_code": item.name, "qty": 1, "rate": 100}]})
	dn.insert(ignore_permissions=True); dn.submit()
	from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment
	sh = make_shipment(dn.name)
	sh.pickup_address_name = sender.name
	sh.pickup_contact_person = "Administrator"
	sh.delivery_address_name = addr.name
	sh.delivery_contact_name = contact
	sh.description_of_content = "API test: IoT controller"
	sh.pickup_date = nowdate()
	sh.value_of_goods = 100
	sh.set("shipment_parcel", [{"length": 20, "width": 15, "height": 10, "weight": 0.5, "count": 1}])
	sh.insert(ignore_permissions=True); sh.submit()
	frappe.db.commit()
	print("CREATED Delivery Note", dn.name, "| Shipment", sh.name)
