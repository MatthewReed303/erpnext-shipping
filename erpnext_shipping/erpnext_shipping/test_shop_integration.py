# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Shop app integration with several warehouses and every shipping provider.

All HTTP calls are mocked; nothing reaches Starshipit, LetMeShip or SendCloud."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from frappe.utils.password import set_encrypted_password

from erpnext_shipping.erpnext_shipping import shop_integration as si

REQUEST = "requests.request"
POST = "requests.post"
EXCHANGE_RATE = "erpnext.setup.utils.get_exchange_rate"
PROVIDERS = ("Starshipit", "LetMeShip", "SendCloud")


def response(payload, status=200):
	mock = MagicMock()
	mock.status_code = status
	mock.ok = 200 <= status < 300
	mock.json.return_value = payload
	mock.headers = {}
	return mock


STARSHIPIT_RATES = {
	"services": [
		{
			"carrier": "CourierPost",
			"carrier_name": "NZ Post Domestic",
			"service_name": "Parcel Post",
			"service_code": "CPOLP",
			"total_price": 9.5,
		}
	],
	"success": True,
}
LETMESHIP_RATES = {
	"serviceList": [
		{
			"baseServiceDetails": {
				"id": 42,
				"carrier": "UPS",
				"name": "Standard",
				"priceInfo": {"realWeight": 1, "netPrice": 10.0},
			}
		}
	]
}
SENDCLOUD_RATES = {
	"data": [
		{
			"carrier": {"name": "DHL"},
			"name": "DHL Parcel",
			"code": "dhl:parcel",
			"quotes": [{"price": {"total": {"value": "7.50", "currency": "EUR"}}}],
		}
	]
}


def starshipit_or_letmeship(method, url, **kwargs):
	"""Starshipit and LetMeShip both call requests.request; answer each by its URL."""
	if "letmeship" in url:
		return response(LETMESHIP_RATES)
	return response(STARSHIPIT_RATES)


class StoreTestCase(IntegrationTestCase):
	def setUp(self):
		self.company = frappe.db.get_value("Company", {}, "name")
		self.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		self.country = frappe.get_cached_value("Company", self.company, "country")
		self.sender = self.address("_Test Store Sender", "1 Main Road", "1010")
		self.box = self.parcel_template()
		self.item = self.weighed_item("_Test Store Parcel Item", 0.4)
		self.pickup_user = self.user("_test_store_pickup@example.com", "+1 555 0100")
		for provider in PROVIDERS:
			frappe.db.set_single_value(
				provider,
				{"enabled": 0, "shop_checkout_rates": 0, "checkout_services": "", "rates_include_tax": 0},
			)
		frappe.db.set_single_value(
			"Starshipit",
			{
				"api_key": "test-key",
				"subscription_key": "test-subscription",
				"sender_address": self.sender,
				"pickup_contact": self.pickup_user,
				"default_parcel_template": None,
			},
		)
		frappe.db.set_single_value("LetMeShip", {"api_id": "test-id", "use_test_environment": 1})
		set_encrypted_password("LetMeShip", "LetMeShip", "test-password", "api_password")
		frappe.db.set_single_value("SendCloud", "api_key", "test-key")
		set_encrypted_password("SendCloud", "SendCloud", "test-secret", "api_secret")
		self.clear()
		sleep = patch("erpnext_shipping.erpnext_shipping.doctype.starshipit.starshipit.time.sleep")
		sleep.start()
		self.addCleanup(sleep.stop)

	def tearDown(self):
		for provider in PROVIDERS:
			frappe.db.set_single_value(provider, {"enabled": 0, "shop_checkout_rates": 0})
		self.clear()

	def clear(self):
		for provider in PROVIDERS:
			frappe.clear_cache(doctype=provider)
		frappe.cache.delete_keys(si.RATE_CACHE_PREFIX)
		frappe.local.cache.clear()  # this process's copy of what was cached

	def offer(self, *providers, **values):
		for provider in providers:
			frappe.db.set_single_value(provider, {"enabled": 1, "shop_checkout_rates": 1, **values})
		self.clear()

	def address(self, title, line1, pincode, country=None, link=None) -> str:
		"""An Address of the company (as a Shipment's pickup needs), or of the party linked."""
		name = f"{title}-Shipping"
		link_doctype, link_name = link or ("Company", self.company)
		if frappe.db.exists("Address", name):
			doc = frappe.get_doc("Address", name)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": title,
					"address_type": "Shipping",
					"address_line1": line1,
					"city": "Testville",
					"pincode": pincode,
					"country": country or self.country,
				}
			)
		if not any(row.link_doctype == link_doctype and row.link_name == link_name for row in doc.links):
			doc.append("links", {"link_doctype": link_doctype, "link_name": link_name})
		doc.is_your_company_address = int(link_doctype == "Company")
		doc.save(ignore_permissions=True)
		return name

	def parcel_template(self) -> str:
		name = "_Test Store Box"
		if not frappe.db.exists("Shipment Parcel Template", name):
			frappe.get_doc(
				{
					"doctype": "Shipment Parcel Template",
					"parcel_template_name": name,
					"length": 20,
					"width": 15,
					"height": 10,
					"weight": 0.1,
				}
			).insert(ignore_permissions=True)
		return name

	def weighed_item(self, code: str, kg: float, domestic_only: int = 0) -> str:
		if not frappe.db.exists("Item", code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"weight_per_unit": kg,
					"weight_uom": "Kg",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Item", code, {"ships_domestic_only": domestic_only, "shipping_parcel_template": self.box}
		)
		return code

	def user(self, email: str, phone: str) -> str:
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Pat",
					"last_name": "Picker",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("User", email, "phone", phone)
		return email

	def context(self, items=None, **values):
		context = frappe._dict(
			cart_items=items or [{"item_code": self.item, "qty": 2, "rate": 100}],
			address=frappe._dict(
				address_line1="5 High St",
				address_line2="",
				city="Elsewhere",
				state="",
				pincode="9016",
				country=self.country,
				country_code=(frappe.db.get_value("Country", self.country, "code") or "").upper(),
			),
			subtotal=200,
			company=self.company,
			currency=self.currency,
		)
		context.update(values)
		return context


class TestCheckoutRates(StoreTestCase):
	@patch(REQUEST)
	def test_another_fulfilment_providers_location_gets_no_rates(self, request):
		self.offer("Starshipit")
		self.assertIsNone(si.checkout_rates(self.context(location_provider="amazon_mcf")))
		self.assertIsNone(si.checkout_rates(self.context(location_provider="manual")))
		request.assert_not_called()

	@patch(REQUEST)
	def test_this_apps_locations_and_old_callers_get_rates(self, request):
		request.return_value = response(STARSHIPIT_RATES)
		self.offer("Starshipit")
		for location_provider in ("erpnext_shipping", "starshipit", None):
			self.clear()
			options = si.checkout_rates(self.context(location_provider=location_provider))["options"]
			self.assertEqual([o["id"] for o in options], ["starshipit:CourierPost|CPOLP"], location_provider)

	@patch(EXCHANGE_RATE, return_value=1.5)
	@patch(POST)
	@patch(REQUEST, side_effect=starshipit_or_letmeship)
	def test_every_provider_offering_store_rates_quotes(self, request, post, _rate):
		post.return_value = response(SENDCLOUD_RATES)
		self.offer(*PROVIDERS)
		options = si.checkout_rates(self.context(currency="NZD", location_provider="erpnext_shipping"))[
			"options"
		]
		by_id = {o["id"]: o for o in options}
		self.assertEqual(
			list(by_id), ["starshipit:CourierPost|CPOLP", "sendcloud:dhl:parcel", "letmeship:42"]
		)  # cheapest first
		# LetMeShip and SendCloud quote in EUR: shown in the store's currency at ERPNext's rate
		self.assertEqual(by_id["sendcloud:dhl:parcel"]["amount"], 11.25)
		self.assertEqual(by_id["letmeship:42"]["amount"], 15.0)
		self.assertEqual(by_id["starshipit:CourierPost|CPOLP"]["amount"], 9.5)
		self.assertEqual(
			by_id["letmeship:42"]["meta"],
			{
				"provider": "LetMeShip",
				"carrier": "UPS",
				"service_id": "42",
				"service_name": "Standard",
				"carrier_code": None,
				"service_code": None,
			},
		)
		self.assertEqual(by_id["sendcloud:dhl:parcel"]["meta"]["provider"], "SendCloud")
		self.assertEqual(by_id["sendcloud:dhl:parcel"]["meta"]["service_id"], "dhl:parcel")
		letmeship = next(c for c in request.call_args_list if "letmeship" in c.args[1])
		# LetMeShip is sent the parcel's box and the pickup contact
		parcel = letmeship.kwargs["json"]["shipmentDetails"]["parcelList"][0]
		self.assertEqual((parcel["length"], parcel["width"], parcel["height"]), (20, 15, 10))
		self.assertEqual(letmeship.kwargs["json"]["pickupInfo"]["person"]["lastname"], "Picker")

	@patch(POST)
	@patch(REQUEST, side_effect=starshipit_or_letmeship)
	def test_each_provider_has_its_own_store_settings(self, request, post):
		self.offer("Starshipit")
		self.offer("SendCloud", rates_include_tax=1, checkout_services="dhl:parcel")
		frappe.db.set_single_value("LetMeShip", "enabled", 1)  # enabled, but not at checkout
		frappe.clear_cache(doctype="LetMeShip")
		post.return_value = response(SENDCLOUD_RATES)
		with patch(EXCHANGE_RATE, return_value=1):
			options = si.checkout_rates(self.context())["options"]
		self.assertEqual({o["meta"]["provider"] for o in options}, {"Starshipit", "SendCloud"})
		self.assertFalse(any("letmeship" in c.args[1] for c in request.call_args_list))
		taxed = {o["meta"]["provider"]: o["tax_included"] for o in options}
		self.assertEqual(taxed, {"Starshipit": False, "SendCloud": True})

		frappe.db.set_single_value("SendCloud", "checkout_services", "postnl:standard")
		self.clear()
		with patch(EXCHANGE_RATE, return_value=1):
			options = si.checkout_rates(self.context())["options"]
		self.assertEqual([o["meta"]["provider"] for o in options], ["Starshipit"])

	@patch(
		REQUEST,
		return_value=response(
			{
				"services": [
					*STARSHIPIT_RATES["services"],
					{
						"carrier": "PlainLabel",
						"carrier_name": "Plain Label",
						"service_name": "Plain Label - My Carrier",
						"service_code": "Plain Label",
						"total_price": 0.0,
					},
				],
				"success": True,
			}
		),
	)
	def test_a_service_priced_at_nothing_is_offered_only_when_the_store_says_it_is_free(self, request):
		self.offer("Starshipit")
		options = si.checkout_rates(self.context())["options"]
		self.assertEqual([o["service"] for o in options], ["Parcel Post"])
		self.offer("Starshipit", checkout_free_services=1)
		options = si.checkout_rates(self.context())["options"]
		self.assertEqual([o["amount"] for o in options], [0.0, 9.5])

		# what the courier's price leaves out is added to a priced service: 10% and 2.00 per parcel
		self.addCleanup(
			frappe.db.set_single_value,
			"Starshipit",
			{"checkout_rate_percent": 0, "checkout_rate_fixed": 0, "checkout_free_services": 0},
		)
		self.offer("Starshipit", checkout_rate_percent=10, checkout_rate_fixed=2)
		options = si.checkout_rates(self.context())["options"]
		self.assertEqual([o["amount"] for o in options], [0.0, 12.45])

	@patch(POST)
	@patch(REQUEST, return_value=response(STARSHIPIT_RATES))
	def test_one_provider_failing_leaves_the_others(self, request, post):
		self.offer("Starshipit", "SendCloud")
		post.return_value = response({"errors": [{"detail": "Service unavailable"}]})
		result = si.checkout_rates(self.context())
		self.assertEqual([o["id"] for o in result["options"]], ["starshipit:CourierPost|CPOLP"])
		self.assertFalse(result.get("messages"))
		si.checkout_rates(self.context())
		self.assertEqual(request.call_count, 1)  # Starshipit's answer was cached
		self.assertEqual(post.call_count, 2)  # SendCloud's failure wasn't

	@patch(REQUEST, return_value=response({"success": False, "errors": [{"message": "Down"}]}, 503))
	def test_no_rates_at_all_says_so(self, request):
		self.offer("Starshipit")
		result = si.checkout_rates(self.context())
		self.assertEqual(result["options"], [])
		self.assertTrue(result["messages"])

	@patch(REQUEST, return_value=response(STARSHIPIT_RATES))
	def test_the_parcels_ship_from_address_is_the_origin(self, request):
		self.offer("Starshipit")
		north = self.address("_Test Store North", "2 North Road", "0610")
		si.checkout_rates(self.context(ship_from=north, warehouse="_Test Store North"))
		self.assertEqual(request.call_args.kwargs["json"]["sender"]["post_code"], "0610")
		si.checkout_rates(self.context())
		self.assertEqual(request.call_args.kwargs["json"]["sender"]["post_code"], "1010")  # the default
		self.assertEqual(request.call_count, 2)  # each origin is cached on its own
		si.checkout_rates(self.context(ship_from=north))
		self.assertEqual(request.call_count, 2)

	def test_no_address_to_send_from_offers_nothing(self):
		self.offer("Starshipit")
		frappe.db.set_single_value("Starshipit", "sender_address", None)
		self.clear()
		self.assertIsNone(si.checkout_rates(self.context()))

	@patch(REQUEST, return_value=response(STARSHIPIT_RATES))
	def test_domestic_only_items_compare_with_the_parcels_origin(self, request):
		self.offer("Starshipit")
		other = frappe.db.get_value("Country", {"name": ["!=", self.country], "code": ["is", "set"]}, "name")
		overseas = self.address("_Test Store Overseas", "3 Far Road", "2000", country=other)
		battery = self.weighed_item("_Test Store Battery", 0.3, domestic_only=1)
		items = [{"item_code": battery, "qty": 1, "rate": 50}]
		# sent from overseas to here: the item would leave the origin's country
		result = si.checkout_rates(self.context(items=items, ship_from=overseas))
		self.assertEqual(result["options"], [])
		self.assertIn(other, result["messages"][0])
		request.assert_not_called()
		# sent within the origin's country
		result = si.checkout_rates(self.context(items=items))
		self.assertTrue(result["options"])


class TestStoreFulfillment(StoreTestCase):
	def setUp(self):
		super().setUp()
		self.north = self.warehouse("_Test Store North")
		self.south = self.warehouse("_Test Store South")
		self.north_address = self.address("_Test Store North", "2 North Road", "0610")
		self.south_address = self.address("_Test Store South", "4 South Road", "9010")
		self.other_item = self.weighed_item("_Test Store Parcel Item 2", 1.0)
		self.north_user = self.user("_test_store_north@example.com", "+1 555 0101")
		frappe.db.set_value("Warehouse", self.north, "shipping_pickup_contact", self.north_user)
		self.receive(self.item, self.north)
		self.receive(self.other_item, self.south)
		self.customer = self.make_customer()

	def warehouse(self, name: str) -> str:
		existing = frappe.db.get_value("Warehouse", {"warehouse_name": name, "company": self.company})
		if existing:
			return existing
		return (
			frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name, "company": self.company})
			.insert(ignore_permissions=True)
			.name
		)

	def receive(self, item: str, warehouse: str, qty: float = 10) -> None:
		entry = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{"item_code": item, "qty": qty, "t_warehouse": warehouse, "basic_rate": 10}],
			}
		)
		entry.insert(ignore_permissions=True)
		entry.submit()

	def make_customer(self) -> str:
		name = "_Test Store Shopper"
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": name,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0})
					or "All Customer Groups",
					"territory": frappe.db.get_value("Territory", {"is_group": 0}) or "All Territories",
				}
			).insert(ignore_permissions=True)
		self.shopper_address = self.address(
			"_Test Store Shopper", "5 High St", "9016", link=("Customer", name)
		)
		return name

	def order(self, *lines):
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer,
				"company": self.company,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 3),
				"currency": self.currency,
				"conversion_rate": 1,
				"shipping_address_name": self.shopper_address,
				"items": [
					{"item_code": item, "qty": qty, "rate": 100, "warehouse": warehouse}
					for item, qty, warehouse in lines
				],
			}
		)
		order.insert(ignore_permissions=True)
		order.submit()
		return order

	def option(self, provider="SendCloud", service_id="dhl:parcel"):
		return json.dumps(
			{
				"id": f"{frappe.scrub(provider)}:{service_id}",
				"label": "DHL Parcel",
				"amount": 11.25,
				"meta": {
					"provider": provider,
					"carrier": "DHL",
					"service_id": service_id,
					"service_name": "DHL Parcel",
				},
			}
		)

	def parcel(self, warehouse, item, qty, ship_from=None, option=None):
		items = [{"item_code": item, "item_name": item, "qty": qty, "warehouse": warehouse}]
		fulfillment = frappe._dict(
			warehouse=warehouse, ship_from_address=ship_from, shipping_option=option, external_id=None
		)
		return items, fulfillment

	def test_both_provider_keys_are_registered(self):
		hooks = frappe.get_hooks("shop_fulfillment_providers")
		self.assertIn("erpnext_shipping", hooks)
		self.assertIn("starshipit", hooks)  # for Shop Fulfillments made before
		self.assertFalse(si.ShippingFulfillment().is_configured())
		frappe.db.set_single_value("SendCloud", "enabled", 1)
		self.assertTrue(si.ShippingFulfillment().is_configured())
		self.assertFalse(si.StarshipitFulfillment().is_configured())  # Starshipit itself is off

	def test_each_parcel_is_delivered_and_shipped_from_its_own_warehouse(self):
		order = self.order((self.item, 2, self.north), (self.other_item, 1, self.south))
		provider = si.ShippingFulfillment()

		items, fulfillment = self.parcel(self.north, self.item, 2, self.north_address, self.option())
		north = provider.create_shipment(order, items, fulfillment)["external_id"]
		items, fulfillment = self.parcel(self.south, self.other_item, 1, self.south_address)
		south = provider.create_shipment(order, items, fulfillment)["external_id"]

		self.assertNotEqual(north, south)
		for shipment, warehouse, address, item, qty, user in (
			(north, self.north, self.north_address, self.item, 2, self.north_user),
			(south, self.south, self.south_address, self.other_item, 1, self.pickup_user),
		):
			doc = frappe.get_doc("Shipment", shipment)
			self.assertEqual(doc.docstatus, 0)  # booking stays with staff
			self.assertEqual(doc.pickup_address_name, address)
			self.assertEqual(doc.pickup_contact_person, user)  # the warehouse's, else Starshipit's
			note = frappe.get_doc("Delivery Note", doc.shipment_delivery_note[0].delivery_note)
			self.assertEqual(
				[(r.item_code, r.qty, r.warehouse) for r in note.items], [(item, qty, warehouse)]
			)
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "per_delivered"), 100)

		choice = json.loads(frappe.db.get_value("Shipment", north, "preferred_service"))
		self.assertEqual((choice["provider"], choice["service_id"]), ("SendCloud", "dhl:parcel"))
		services = [
			frappe._dict(service_provider="SendCloud", service_id="dhl:parcel", service_name="DHL Parcel"),
			frappe._dict(service_provider="Starshipit", service_id="dhl:parcel", service_name="DHL Parcel"),
		]
		self.assertEqual([s.get("is_preferred") for s in si.mark_preferred(services, north)], [1, None])

		# the same parcel sent again gets its Shipment back
		items, fulfillment = self.parcel(self.north, self.item, 2, self.north_address)
		fulfillment.external_id = north
		self.assertEqual(provider.create_shipment(order, items, fulfillment)["external_id"], north)

		# cancelling one parcel puts back only its stock
		provider.cancel(frappe._dict(external_id=north))
		self.assertFalse(frappe.db.exists("Shipment", north))
		delivered = dict(
			frappe.get_all(
				"Sales Order Item",
				filters={"parent": order.name},
				fields=["item_code", "delivered_qty"],
				as_list=True,
			)
		)
		self.assertEqual(delivered, {self.item: 0, self.other_item: 1})
		self.assertEqual(
			provider.fetch_status(frappe._dict(external_id=south, status="Accepted"))["status"], "Accepted"
		)

	def test_a_parcel_takes_only_what_is_left_to_deliver(self):
		order = self.order((self.item, 1, self.north))
		items, fulfillment = self.parcel(self.north, self.item, 3)
		with self.assertRaises(frappe.ValidationError):
			si.ShippingFulfillment().create_shipment(order, items, fulfillment)

	def test_an_order_sent_whole_is_delivered_whole(self):
		# Shop before fulfilment locations: no warehouse, no fulfilment record for the parcel
		order = self.order((self.item, 2, self.north), (self.other_item, 1, self.south))
		items = [
			{"item_code": self.item, "item_name": self.item, "qty": 2},
			{"item_code": self.other_item, "item_name": self.other_item, "qty": 1},
		]
		shipment = si.StarshipitFulfillment().create_shipment(order, items)["external_id"]
		doc = frappe.get_doc("Shipment", shipment)
		self.assertEqual(doc.pickup_address_name, self.sender)
		note = frappe.get_doc("Delivery Note", doc.shipment_delivery_note[0].delivery_note)
		self.assertEqual(len(note.items), 2)
		# asked again, the order's Shipment is found rather than made twice
		self.assertEqual(si.StarshipitFulfillment().create_shipment(order, items)["external_id"], shipment)

	def test_the_pending_fulfilment_is_found_when_shop_passes_only_the_items(self):
		if not frappe.get_meta("Shop Fulfillment").has_field("warehouse"):
			self.skipTest("Shop has no fulfilment locations")
		order = self.order((self.item, 2, self.north), (self.other_item, 1, self.south))
		records = {}
		for warehouse, item, qty, address in (
			(self.north, self.item, 2, self.north_address),
			(self.south, self.other_item, 1, self.south_address),
		):
			records[warehouse] = frappe.get_doc(
				{
					"doctype": "Shop Fulfillment",
					"sales_order": order.name,
					"provider": "erpnext_shipping",
					"status": "Pending",
					"warehouse": warehouse,
					"ship_from_address": address,
					"shipping_option": self.option() if warehouse == self.south else None,
					"items": [{"item_code": item, "item_name": item, "qty": qty}],
				}
			).insert(ignore_permissions=True)
		items = [
			{"item_code": self.other_item, "item_name": self.other_item, "qty": 1, "warehouse": self.south}
		]
		found = si.pending_fulfillment(order.name, "erpnext_shipping", items)
		self.assertEqual(found.name, records[self.south].name)
		shipment = si.ShippingFulfillment().create_shipment(order, items)["external_id"]
		self.assertEqual(frappe.db.get_value("Shipment", shipment, "pickup_address_name"), self.south_address)

		records[self.south].db_set("external_id", shipment)
		records[self.north].db_set("external_id", "SHIP-ELSEWHERE")
		with patch("shop.fulfillment.service.sync") as sync:
			si.sync_store_fulfillment(shipment)
		sync.assert_called_once_with(records[self.south].name)
