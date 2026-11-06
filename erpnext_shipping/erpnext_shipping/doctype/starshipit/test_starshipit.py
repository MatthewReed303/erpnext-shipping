# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Starshipit tests. All HTTP calls are mocked; nothing reaches Starshipit."""

import base64
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import (
	STARSHIPIT_PROVIDER,
	StarshipitUtils,
	normalize_status,
	split_service_id,
	tracking_dict,
)

# The settings link to Shipment Parcel Template; its sample records need a full ERPNext test setup
# and are not used here.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Shipment Parcel Template"]

REQUESTS = "erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit.requests.request"


def response(payload, status=200):
	mock = MagicMock()
	mock.status_code = status
	mock.ok = 200 <= status < 300
	mock.json.return_value = payload
	return mock


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


class TestStarshipitHelpers(UnitTestCase):
	def test_normalize_status(self):
		self.assertEqual(normalize_status("InTransit"), "In Transit")
		self.assertEqual(normalize_status("OutForDelivery"), "Out For Delivery")
		self.assertEqual(normalize_status("Delivered"), "Delivered")
		self.assertEqual(normalize_status(None), "")

	def test_split_service_id(self):
		self.assertEqual(split_service_id({"service_id": "NZPost|CPOLP"}), ("NZPost", "CPOLP"))

	def test_tracking_dict(self):
		self.assertIsNone(tracking_dict({}))
		result = tracking_dict(
			{
				"tracking_number": "ABC123",
				"tracking_status": "InTransit",
				"tracking_url": "https://track.example/ABC123",
				"tracking_events": [{"details": "Picked up"}, {"details": "At depot"}],
			}
		)
		self.assertEqual(result["awb_number"], "ABC123")
		self.assertEqual(result["tracking_status"], "In Transit")
		self.assertEqual(result["tracking_status_info"], "At depot")


class TestStarshipit(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single("Starshipit")
		settings.enabled = 1
		settings.api_key = "test-key"
		settings.subscription_key = "test-subscription"
		settings.allow_label_printing = 0
		settings.default_parcel_template = None
		settings.save()

	def tearDown(self):
		frappe.db.set_single_value("Starshipit", "enabled", 0)

	def test_webhook_token_and_url(self):
		settings = frappe.get_single("Starshipit")
		self.assertTrue(settings.webhook_token)
		self.assertIn(settings.webhook_token, settings.webhook_url)

	@patch(REQUESTS)
	def test_rates_use_delivery_services_in_metres(self, request):
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
			pickup_address=address(),
			delivery_address=address(city="Christchurch", pincode="8011"),
			parcels=[{"length": 30, "width": 20, "height": 10, "weight": 1.2, "count": 2}],
			value_of_goods=150,
		)

		method, url = request.call_args.args
		payload = request.call_args.kwargs["json"]
		headers = request.call_args.kwargs["headers"]
		self.assertEqual((method, url), ("POST", "https://api.starshipit.com/api/deliveryservices"))
		self.assertEqual(headers["StarShipIT-Api-Key"], "test-key")
		self.assertEqual(headers["Ocp-Apim-Subscription-Key"], "test-subscription")
		self.assertEqual(payload["destination"]["country_code"], "NZ")
		self.assertEqual(len(payload["packages"]), 2)
		self.assertEqual(payload["packages"][0], {"weight": 1.2, "length": 0.3, "width": 0.2, "height": 0.1})
		self.assertTrue(payload["include_pricing"])

		self.assertEqual([r.service_name for r in rates], ["Economy", "Overnight"])
		self.assertEqual(rates[0].service_provider, STARSHIPIT_PROVIDER)
		self.assertEqual(rates[0].service_id, "NZPost|CPOLE")
		self.assertEqual(rates[0].carrier, "NZ Post")

	@patch(REQUESTS)
	def test_api_errors_are_shown(self, request):
		request.return_value = response(
			{"success": False, "errors": [{"message": "Invalid post code", "details": "8011x"}]}, status=400
		)
		with self.assertRaises(frappe.ValidationError) as context:
			StarshipitUtils().get_available_services(address(), address(), [{"weight": 1}])
		self.assertIn("Invalid post code", str(context.exception))

	@patch(REQUESTS)
	def test_label_printing_refused_unless_allowed(self, request):
		shipment = frappe._dict(shipment_id="123", awb_number="")
		with self.assertRaises(frappe.ValidationError):
			StarshipitUtils().print_label(shipment)
		request.assert_not_called()

	@patch(REQUESTS)
	def test_reprint_is_always_allowed(self, request):
		pdf = b"%PDF-1.4 test"
		request.return_value = response(
			{"labels": [base64.b64encode(pdf).decode()], "tracking_numbers": ["TN1"], "success": True}
		)
		labels, tracking_numbers = StarshipitUtils().print_label(
			frappe._dict(shipment_id="123", awb_number="TN1")
		)
		self.assertEqual(labels, [pdf])
		self.assertEqual(tracking_numbers, ["TN1"])
		self.assertEqual(request.call_args.kwargs["json"], {"order_id": 123, "reprint": True})

	@patch(REQUESTS)
	def test_label_printed_when_allowed(self, request):
		frappe.db.set_single_value("Starshipit", "allow_label_printing", 1)
		request.return_value = response({"labels": [], "tracking_numbers": ["TN2"], "success": True})
		_labels, tracking_numbers = StarshipitUtils().print_label(
			frappe._dict(shipment_id="456", awb_number="")
		)
		self.assertEqual(tracking_numbers, ["TN2"])
		self.assertEqual(request.call_args.kwargs["json"], {"order_id": 456, "reprint": False})

	@patch(REQUESTS)
	def test_cancel_deletes_unprinted_and_archives_printed(self, request):
		request.return_value = response({"success": True})
		self.assertEqual(StarshipitUtils().cancel_order("123"), "deleted")
		self.assertEqual(request.call_args.args, ("DELETE", "https://api.starshipit.com/api/orders/delete"))
		self.assertEqual(StarshipitUtils().cancel_order("123", printed=True), "archived")
		self.assertEqual(request.call_args.args, ("POST", "https://api.starshipit.com/api/orders/archive"))
		self.assertEqual(request.call_args.kwargs["params"], {"order_id": "123"})

	@patch(REQUESTS)
	def test_tracking_lookup_by_order_number(self, request):
		request.return_value = response(
			{
				"results": {"tracking_number": "TN3", "tracking_status": "Delivered", "tracking_events": []},
				"success": True,
			}
		)
		data = StarshipitUtils().get_tracking_data("SHIP-0001")
		self.assertEqual(request.call_args.kwargs["params"], {"order_number": "SHIP-0001"})
		self.assertEqual(data["tracking_status"], "Delivered")

	@patch(REQUESTS)
	def test_tracking_before_label_returns_none(self, request):
		request.return_value = response({"success": False, "errors": [{"message": "Not found"}]}, status=404)
		self.assertIsNone(StarshipitUtils().get_tracking_data("SHIP-0002"))

	def test_webhook_rejects_wrong_token(self):
		from erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit import webhook

		with self.assertRaises(frappe.PermissionError):
			webhook(token="wrong")

	def test_service_list_json_round_trip(self):
		# service dicts are sent to the browser and back as JSON when a service is chosen
		service = StarshipitUtils.service_dict(
			{
				"carrier": "Aramex",
				"carrier_name": "Aramex",
				"service_name": "Parcel",
				"service_code": "P",
				"total_price": 5,
			}
		)
		self.assertEqual(json.loads(json.dumps(service))["service_id"], "Aramex|P")

	def create_test_shipment(self, request):
		company = frappe.db.get_value("Company", {}, "name")
		shipment = frappe._dict(name="SHIP-TEST-1", company=company, shipment_delivery_note=[])
		original_get_doc = frappe.get_doc

		def get_doc(*args, **kwargs):
			if args and args[0] == "Shipment":
				return shipment
			return original_get_doc(*args, **kwargs)

		with patch("frappe.get_doc", side_effect=get_doc):
			return StarshipitUtils().create_shipment(
				shipment="SHIP-TEST-1",
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
					"service_id": "NZPost|CPOLP",
					"service_name": "Overnight",
					"carrier": "NZ Post",
					"total_price": 9.5,
				},
			)

	@patch(REQUESTS)
	def test_create_shipment_creates_the_order_without_a_label(self, request):
		request.side_effect = [
			response({"success": False, "errors": [{"message": "Order not found"}]}, status=404),
			response({"order": {"order_id": 987654, "carrier_name": "NZ Post"}, "success": True}),
		]
		info = self.create_test_shipment(request)

		self.assertEqual(request.call_count, 2)
		lookup, create = request.call_args_list
		self.assertEqual(lookup.args, ("GET", "https://api.starshipit.com/api/orders"))
		self.assertEqual(lookup.kwargs["params"], {"order_number": "SHIP-TEST-1"})
		self.assertEqual(create.args, ("POST", "https://api.starshipit.com/api/orders"))
		order = create.kwargs["json"]["order"]
		self.assertEqual(order["order_number"], "SHIP-TEST-1")
		self.assertEqual((order["carrier"], order["carrier_service_code"]), ("NZPost", "CPOLP"))
		self.assertEqual(order["destination"]["name"], "Ann Lee")
		self.assertEqual(order["destination"]["company"], "Lee Farms")
		self.assertEqual(order["destination"]["country"], "New Zealand")
		self.assertEqual(order["items"][0]["description"], "IoT controller")
		self.assertEqual(order["packages"], [{"weight": 0.8, "length": 0.2, "width": 0.15, "height": 0.1}])
		self.assertEqual(info["shipment_id"], "987654")
		self.assertEqual(info["service_provider"], STARSHIPIT_PROVIDER)
		self.assertEqual(info["awb_number"], "")

	@patch(REQUESTS)
	def test_repeat_create_updates_instead_of_duplicating(self, request):
		request.side_effect = [
			response({"order": {"order_id": 555, "order_number": "SHIP-TEST-1"}, "success": True}),
			response({"order": {"order_id": 555}, "success": True}),
		]
		info = self.create_test_shipment(request)

		methods = [call.args[0] for call in request.call_args_list]
		self.assertEqual(methods, ["GET", "PUT"])
		self.assertEqual(request.call_args.kwargs["json"]["order"]["order_id"], 555)
		self.assertEqual(info["shipment_id"], "555")

	@patch("erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit.time.sleep")
	@patch(REQUESTS)
	def test_rate_limit_is_retried_once(self, request, sleep):
		limited = response({}, status=429)
		limited.headers = {"Retry-After": "1"}
		request.side_effect = [limited, response({"orders": [], "success": True})]
		StarshipitUtils().request("GET", "/orders/unshipped")
		self.assertEqual(request.call_count, 2)
		sleep.assert_called_once_with(1)
