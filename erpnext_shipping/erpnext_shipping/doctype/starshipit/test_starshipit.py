# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Starshipit tests. All HTTP calls are mocked; nothing reaches Starshipit.

Request and response shapes follow https://support.starshipit.com/developers/api-reference/starshipit
"""

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import (
	STARSHIPIT_PROVIDER,
	StarshipitUtils,
	erpnext_tracking_status,
	kg_per_unit,
	local_to_utc,
	normalize_status,
	split_service_id,
	status_dict,
	valid_signature,
)

# The settings link to Shipment Parcel Template; its sample records need a full ERPNext test setup
# and are not used here.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Shipment Parcel Template"]

MODULE = "erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit"
REQUESTS = MODULE + ".requests.request"
API = "https://api.starshipit.com/api"


_original_system_setting = frappe.get_system_settings


def _system_setting(key):
	return _original_system_setting(key)


def response(payload, status=200):
	mock = MagicMock()
	mock.status_code = status
	mock.ok = 200 <= status < 300
	mock.json.return_value = payload
	mock.headers = {}
	return mock


def not_found():
	return response({"success": False, "errors": [{"message": "Order not found"}]}, status=404)


def address(**values):
	data = {
		"address_title": "Test",
		"address_line1": "1 Queen Street",
		"address_line2": "Auckland Central",
		"city": "Auckland",
		"state": "",
		"pincode": "1010",
		"country": "New Zealand",
		"country_code": "nz",
	}
	data.update(values)
	return frappe._dict(data)


def calls(request):
	return [(c.args[0], c.args[1].replace(API, "")) for c in request.call_args_list]


class TestStarshipitHelpers(UnitTestCase):
	def test_normalize_status(self):
		self.assertEqual(normalize_status("InTransit"), "In Transit")
		self.assertEqual(normalize_status("OutForDelivery"), "Out For Delivery")
		self.assertEqual(normalize_status(None), "")

	def test_tracking_status_uses_shipment_options(self):
		# Shipment.tracking_status only allows "", In Progress, Delivered, Returned, Lost
		self.assertEqual(erpnext_tracking_status("Printed"), "In Progress")
		self.assertEqual(erpnext_tracking_status("InTransit"), "In Progress")
		self.assertEqual(erpnext_tracking_status("Delivered"), "Delivered")
		self.assertEqual(erpnext_tracking_status(""), "")
		self.assertEqual(erpnext_tracking_status("Saved"), "")  # no label yet
		self.assertEqual(erpnext_tracking_status("Unshipped"), "")

	def test_status_from_order_and_tracking(self):
		printed = status_dict({"status": "Printed", "packages": [{"tracking_number": None}]})
		self.assertEqual(
			(printed["tracking_status"], printed["tracking_status_info"]), ("In Progress", "Printed")
		)
		manifested = status_dict({"status": "Printed", "manifested": True})
		self.assertEqual(manifested["tracking_status_info"], "Manifested")
		delivered = status_dict(
			{"status": "Printed"},
			{
				"tracking_number": "TN1",
				"tracking_status": "Delivered",
				"tracking_url": "https://t/TN1",
				"tracking_events": [{"details": "Signed for"}],
			},
		)
		self.assertEqual(delivered["tracking_status"], "Delivered")
		self.assertEqual(delivered["tracking_status_info"], "Delivered: Signed for")
		self.assertEqual((delivered["awb_number"], delivered["tracking_url"]), ("TN1", "https://t/TN1"))

	def test_split_service_id(self):
		self.assertEqual(split_service_id({"service_id": "NZPost|CPOLP"}), ("NZPost", "CPOLP"))

	def test_kg_per_unit(self):
		self.assertEqual(kg_per_unit(0.25, "Kg"), 0.25)
		self.assertEqual(kg_per_unit(250, "Gram"), 0.25)
		self.assertIsNone(kg_per_unit(1, "Nos"))
		self.assertIsNone(kg_per_unit(0, "Kg"))

	def test_webhook_signature(self):
		body = b'{"order_number":"SHIP-1","tracking_status":"Delivered"}'
		signature = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
		self.assertTrue(valid_signature(body, signature, "s3cret"))
		self.assertTrue(valid_signature(body, signature.upper(), "s3cret"))
		self.assertFalse(valid_signature(body, signature, "other"))
		self.assertFalse(valid_signature(body, None, "s3cret"))

	def test_pickup_window_to_utc(self):
		# NZ daylight time (UTC+13) in November
		self.assertEqual(local_to_utc("2026-11-10", "09:00:00", "Pacific/Auckland"), "2026-11-09T20:00:00Z")


class TestStarshipit(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single("Starshipit")
		settings.enabled = 1
		settings.api_key = "test-key"
		settings.subscription_key = "test-subscription"
		settings.allow_label_printing = 0
		settings.default_parcel_template = None
		settings.webhook_secret = None
		settings.save()
		sleep = patch(MODULE + ".time.sleep")
		sleep.start()
		self.addCleanup(sleep.stop)

	def tearDown(self):
		frappe.db.set_single_value("Starshipit", "enabled", 0)

	def shipment(self, **values):
		# a real (unsaved) Shipment, so the code can only use fields Shipment actually has
		doc = frappe.new_doc("Shipment")
		doc.name = values.pop("name", "SHIP-TEST-1")
		doc.pickup_from_type = "Company"
		doc.pickup_company = frappe.db.get_value("Company", {}, "name")
		doc.update(values)
		return doc

	def create(self, request, shipment):
		original_get_doc = frappe.get_doc

		def get_doc(*args, **kwargs):
			if args and args[0] == "Shipment":
				return shipment
			return original_get_doc(*args, **kwargs)

		with patch("frappe.get_doc", side_effect=get_doc):
			return StarshipitUtils().create_shipment(
				shipment=shipment.name,
				pickup_address=address(),
				pickup_contact=frappe._dict(
					first_name="Mat", last_name="Reed", email_id="a@b.nz", phone="+6421"
				),
				delivery_address=address(address_line1="5 High St", city="Dunedin", pincode="9016"),
				delivery_contact=frappe._dict(first_name="Ann", last_name="Lee", email_id="ann@example.com"),
				delivery_company_name="Lee Farms",
				shipment_parcel=json.dumps(
					[{"length": 20, "width": 15, "height": 10, "weight": 0.8, "count": 1}]
				),
				description_of_content="IoT controller",
				value_of_goods=250,
				service_info={
					"service_id": "PlainLabel|Plain Label",
					"service_name": "Plain Label - My Carrier",
					"carrier": "Plain Label",
					"total_price": 0,
				},
			)

	def test_webhook_url_has_token(self):
		settings = frappe.get_single("Starshipit")
		self.assertIn(settings.webhook_token, settings.webhook_url)

	@patch(REQUESTS)
	def test_rates_from_delivery_services_in_metres(self, request):
		request.return_value = response(
			{
				"services": [
					{
						"carrier": "NZPost",
						"carrier_name": "NZ Post",
						"service_name": "Overnight",
						"service_code": "CPOLP",
						"total_price": 9.5,
					},
					{
						"carrier": "NZPost",
						"carrier_name": "NZ Post",
						"service_name": "Economy",
						"service_code": "CPOLE",
						"total_price": 7.2,
					},
				],
				"success": True,
			}
		)
		rates = StarshipitUtils().get_available_services(
			address(),
			address(city="Christchurch", pincode="8011"),
			[{"length": 30, "width": 20, "height": 10, "weight": 1.2, "count": 2}],
			value_of_goods=150,
		)
		self.assertEqual(calls(request), [("POST", "/deliveryservices")])
		payload = request.call_args.kwargs["json"]
		headers = request.call_args.kwargs["headers"]
		self.assertEqual(headers["StarShipIT-Api-Key"], "test-key")
		self.assertEqual(headers["Ocp-Apim-Subscription-Key"], "test-subscription")
		self.assertEqual(payload["destination"]["country_code"], "NZ")
		self.assertEqual(len(payload["packages"]), 2)
		self.assertEqual(
			payload["packages"][0],
			{"name": "Parcel 1", "weight": 1.2, "length": 0.3, "width": 0.2, "height": 0.1},
		)
		self.assertEqual([r.service_name for r in rates], ["Economy", "Overnight"])
		self.assertEqual((rates[0].service_id, rates[0].carrier), ("NZPost|CPOLE", "NZ Post"))

	@patch(REQUESTS)
	def test_api_errors_are_shown(self, request):
		request.return_value = response(
			{"success": False, "errors": [{"message": "Invalid post code", "details": "8011x"}]}, status=400
		)
		with self.assertRaises(frappe.ValidationError) as context:
			StarshipitUtils().get_available_services(address(), address(), [{"weight": 1}])
		self.assertIn("Invalid post code", str(context.exception))

	@patch(REQUESTS)
	def test_create_order_then_set_courier(self, request):
		request.side_effect = [
			not_found(),
			response({"order": {"order_id": 987654}, "success": True}),
			response({"order": {"order_id": 987654, "carrier": "PlainLabel"}, "success": True}),
		]
		info = self.create(request, self.shipment())

		self.assertEqual(calls(request), [("GET", "/orders"), ("POST", "/orders"), ("PUT", "/orders")])
		lookup, create, set_courier = request.call_args_list
		self.assertEqual(lookup.kwargs["params"], {"order_number": "SHIP-TEST-1"})
		order = create.kwargs["json"]["order"]
		# Create Order - Order Model has no courier fields
		self.assertNotIn("carrier", order)
		self.assertNotIn("carrier_service_code", order)
		self.assertEqual(order["order_number"], "SHIP-TEST-1")
		self.assertEqual(order["shipping_method"], "Plain Label - My Carrier")
		self.assertEqual(order["destination"]["name"], "Ann Lee")
		self.assertEqual(order["destination"]["company"], "Lee Farms")
		self.assertEqual(order["sender_details"]["name"], "Mat Reed")
		self.assertEqual(order["items"][0]["description"], "IoT controller")
		self.assertEqual(
			order["packages"],
			[{"name": "Parcel 1", "weight": 0.8, "length": 0.2, "width": 0.15, "height": 0.1}],
		)
		self.assertEqual(
			set_courier.kwargs["json"],
			{"order": {"order_id": 987654, "carrier": "PlainLabel", "carrier_service_code": "Plain Label"}},
		)
		self.assertEqual((info["shipment_id"], info["service_provider"]), ("987654", STARSHIPIT_PROVIDER))

	@patch(REQUESTS)
	def test_repeat_create_updates_instead_of_duplicating(self, request):
		request.side_effect = [
			response({"order": {"order_id": 555, "status": "Saved"}, "success": True}),
			response({"order": {"order_id": 555}, "success": True}),
			response({"order": {"order_id": 555}, "success": True}),
		]
		info = self.create(request, self.shipment())
		self.assertEqual(calls(request), [("GET", "/orders"), ("PUT", "/orders"), ("PUT", "/orders")])
		self.assertEqual(info["shipment_id"], "555")

	@patch(REQUESTS)
	def test_repeat_create_leaves_printed_order_alone(self, request):
		request.side_effect = [response({"order": {"order_id": 556, "status": "Printed"}, "success": True})]
		info = self.create(request, self.shipment())
		self.assertEqual(calls(request), [("GET", "/orders")])
		self.assertEqual(info["shipment_id"], "556")

	@patch(REQUESTS)
	def test_label_refused_unless_allowed(self, request):
		request.return_value = response({"order": {"order_id": 123, "status": "Saved"}, "success": True})
		with self.assertRaises(frappe.ValidationError):
			StarshipitUtils().print_label(self.shipment(shipment_id="123"))
		self.assertEqual(calls(request), [("GET", "/orders")])

	@patch(REQUESTS)
	def test_label_sends_the_order_courier(self, request):
		frappe.db.set_single_value("Starshipit", "allow_label_printing", 1)
		pdf = b"%PDF-1.4 label"
		request.side_effect = [
			response(
				{
					"order": {
						"order_id": 456,
						"status": "Saved",
						"carrier": "PlainLabel",
						"carrier_service_code": "Plain Label",
					},
					"success": True,
				}
			),
			response(
				{"labels": [base64.b64encode(pdf).decode()], "tracking_numbers": [None], "success": True}
			),
		]
		labels, tracking_numbers = StarshipitUtils().print_label(self.shipment(shipment_id="456"))
		self.assertEqual(calls(request), [("GET", "/orders"), ("POST", "/orders/shipment")])
		self.assertEqual(
			request.call_args.kwargs["json"],
			{
				"order_id": 456,
				"reprint": False,
				"carrier": "PlainLabel",
				"carrier_service_code": "Plain Label",
			},
		)
		self.assertEqual((labels, tracking_numbers), ([pdf], []))  # plain labels have no tracking number

	@patch(REQUESTS)
	def test_printed_order_is_reprinted_not_booked_again(self, request):
		request.side_effect = [
			response({"order": {"order_id": 789, "status": "Printed"}, "success": True}),
			response({"labels": [], "tracking_numbers": [], "success": True}),
		]
		StarshipitUtils().print_label(self.shipment(shipment_id="789"))
		self.assertEqual(request.call_args.kwargs["json"], {"order_id": 789, "reprint": True})

	@patch(REQUESTS)
	def test_manifest(self, request):
		pdf = b"%PDF manifest"
		request.side_effect = [
			response({"order": {"order_id": 321, "status": "Printed"}, "success": True}),
			response(
				{"label_type": "ManifestReport", "pdf": base64.b64encode(pdf).decode(), "success": True}
			),
		]
		self.assertEqual(StarshipitUtils().manifest(self.shipment(shipment_id="321")), pdf)
		self.assertEqual(calls(request)[-1], ("POST", "/orders/manifest"))
		self.assertEqual(request.call_args.kwargs["json"], {"order_ids": [321]})

	@patch(REQUESTS)
	def test_manifest_only_once(self, request):
		request.return_value = response(
			{"order": {"order_id": 323, "status": "Printed", "manifested": True}, "success": True}
		)
		with self.assertRaises(frappe.ValidationError):
			StarshipitUtils().manifest(self.shipment(shipment_id="323"))
		self.assertNotIn(("POST", "/orders/manifest"), calls(request))

	@patch(REQUESTS)
	def test_manifest_needs_a_label(self, request):
		request.return_value = response({"order": {"order_id": 322, "status": "Saved"}, "success": True})
		with self.assertRaises(frappe.ValidationError):
			StarshipitUtils().manifest(self.shipment(shipment_id="322"))
		self.assertNotIn(("POST", "/orders/manifest"), calls(request))

	@patch(REQUESTS)
	def test_commercial_invoice(self, request):
		pdf = b"%PDF invoice"
		request.return_value = response(
			{
				"invoiceList": [{"order_id": 111, "invoice_base64_string": base64.b64encode(pdf).decode()}],
				"success": True,
			}
		)
		self.assertEqual(StarshipitUtils().commercial_invoices(self.shipment(shipment_id="111")), [pdf])
		self.assertEqual(request.call_args.kwargs["json"], {"order_ids": [111]})

	@patch(
		MODULE + ".frappe.get_system_settings",
		side_effect=lambda key=None: "Pacific/Auckland" if key == "time_zone" else _system_setting(key),
	)
	@patch(REQUESTS)
	def test_book_pickup(self, request, _zone):
		request.side_effect = [
			response(
				{
					"order": {
						"order_id": 654,
						"status": "Printed",
						"carrier_name": "Aramex",
						"carrier_service_code": "P",
						"sender_details": {
							"name": "Mat",
							"street": "1 Queen Street",
							"city": "Auckland",
							"post_code": "1010",
							"country": "New Zealand",
						},
						"destination": {
							"name": "Ann",
							"street": "5 High St",
							"city": "Dunedin",
							"post_code": "9016",
							"country": "New Zealand",
						},
						"packages": [
							{"weight": 0.8, "length": 0.2, "width": 0.15, "height": 0.1},
							{"weight": 1.5, "length": 0.4, "width": 0.3, "height": 0.2},
						],
					},
					"success": True,
				}
			),
			response({"job_number": "4500", "success": True}),
		]
		shipment = self.shipment(
			shipment_id="654",
			awb_number="TN9",
			pickup_date="2026-11-10",
			pickup_from="09:00:00",
			pickup_to="12:00:00",
		)
		self.assertEqual(StarshipitUtils().book_pickup(shipment, "Side door"), "4500")
		self.assertEqual(calls(request)[-1], ("POST", "/pickup/book"))
		payload = request.call_args.kwargs["json"]
		self.assertEqual(payload["carrier_name"], "Aramex")
		self.assertEqual(payload["tracking_number"], "TN9")
		self.assertEqual(payload["pickup_timeslot_start"], "2026-11-09T20:00:00Z")
		self.assertEqual(payload["pickup_timeslot_end"], "2026-11-09T23:00:00Z")
		self.assertEqual(payload["pickup_timezone_id"], "New Zealand Standard Time")
		self.assertEqual(payload["collection_address"]["address_line1"], "1 Queen Street")
		self.assertEqual(payload["package_count"], 2)
		self.assertEqual(payload["total_weight"], 2.3)
		# pickup sizes are in centimetres
		self.assertEqual(
			(payload["max_package_length"], payload["max_package_width"], payload["max_package_height"]),
			(40.0, 30.0, 20.0),
		)

	@patch(REQUESTS)
	def test_pickup_needs_a_label(self, request):
		request.return_value = response({"order": {"order_id": 655, "status": "Saved"}, "success": True})
		shipment = self.shipment(
			shipment_id="655", pickup_date="2026-11-10", pickup_from="09:00:00", pickup_to="12:00:00"
		)
		with self.assertRaises(frappe.ValidationError):
			StarshipitUtils().book_pickup(shipment)

	@patch(REQUESTS)
	def test_cancel_deletes_unprinted_and_archives_printed(self, request):
		request.return_value = response({"success": True})
		self.assertEqual(StarshipitUtils().cancel_order("123"), "deleted")
		self.assertEqual(calls(request)[-1], ("DELETE", "/orders/delete"))
		self.assertEqual(StarshipitUtils().cancel_order("123", printed=True), "archived")
		self.assertEqual(calls(request)[-1], ("POST", "/orders/archive"))
		self.assertEqual(request.call_args.kwargs["params"], {"order_id": "123"})

	@patch(REQUESTS)
	def test_status_from_order_when_no_courier_tracking(self, request):
		request.side_effect = [
			response({"order": {"order_id": 9, "status": "Printed", "manifested": True}, "success": True}),
			not_found(),
		]
		data = StarshipitUtils().get_tracking_data("SHIP-TEST-1")
		self.assertEqual(calls(request), [("GET", "/orders"), ("GET", "/track")])
		self.assertEqual(
			(data["tracking_status"], data["tracking_status_info"]), ("In Progress", "Manifested")
		)

	@patch(REQUESTS)
	def test_rate_limit_is_retried_once(self, request):
		limited = response({}, status=429)
		limited.headers = {"Retry-After": "1"}
		request.side_effect = [limited, response({"orders": [], "success": True})]
		StarshipitUtils().request("GET", "/orders/unshipped")
		self.assertEqual(request.call_count, 2)

	def webhook_request(self, body: bytes, query: dict | None = None, headers: dict | None = None):
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		builder = EnvironBuilder(
			method="POST",
			path="/",
			query_string=query or {},
			data=body,
			headers=headers or {},
			content_type="application/json",
		)
		frappe.local.request = Request(builder.get_environ())
		self.addCleanup(lambda: hasattr(frappe.local, "request") and delattr(frappe.local, "request"))

	def test_webhook_token_in_url(self):
		from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import webhook

		body = json.dumps({"order_number": "NO-SUCH-SHIPMENT", "tracking_status": "Dispatched"}).encode()
		self.webhook_request(body, {"token": "wrong"})
		with self.assertRaises(frappe.PermissionError):
			webhook()
		self.webhook_request(body, {"token": frappe.db.get_single_value("Starshipit", "webhook_token")})
		self.assertEqual(webhook(), {"ok": True, "matched": False})

	def test_webhook_signature_when_secret_set(self):
		from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import webhook

		settings = frappe.get_single("Starshipit")
		settings.webhook_secret = "s3cret"
		settings.save()
		body = json.dumps({"order_number": "NO-SUCH-SHIPMENT", "tracking_status": "Dispatched"}).encode()
		token = frappe.db.get_single_value("Starshipit", "webhook_token")
		self.webhook_request(body, {"token": token})  # a token alone is not enough once a secret is set
		with self.assertRaises(frappe.PermissionError):
			webhook()
		signature = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
		self.webhook_request(body, headers={"X-Starshipit-Signature": signature})
		self.assertEqual(webhook(), {"ok": True, "matched": False})
