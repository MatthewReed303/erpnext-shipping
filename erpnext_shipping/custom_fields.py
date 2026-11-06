from .utils import identity as _


def get_custom_fields():
	return {
		"Item": [
			{
				"fieldname": "shipping_parcel_template",
				"label": _("Parcel Template"),
				"fieldtype": "Link",
				"options": "Shipment Parcel Template",
				"description": _("Box this item ships in"),
				"depends_on": "eval:doc.is_stock_item",
				"insert_after": "weight_uom",
			},
			{
				"fieldname": "ships_in_own_box",
				"label": _("Ships in Own Box"),
				"fieldtype": "Check",
				"description": _("Never packed with other items"),
				"depends_on": "eval:doc.is_stock_item",
				"insert_after": "shipping_parcel_template",
			},
		],
		"Delivery Note": [
			{
				"fieldname": "shipping_sec_break",
				"label": _("Shipping Details"),
				"fieldtype": "Section Break",
				"collapsible": 1,
				"insert_after": "sales_team",
			},
			{
				"fieldname": "delivery_type",
				"label": _("Delivery Type"),
				"fieldtype": "Data",
				"read_only": 1,
				"translatable": 0,
				"insert_after": "shipping_sec_break",
			},
			{
				"fieldname": "parcel_service",
				"label": _("Parcel Service"),
				"fieldtype": "Data",  # needs to be "Data" for backward compat
				"options": "Parcel Service",
				"read_only": 1,
				"insert_after": "delivery_type",
			},
			{
				"fieldname": "parcel_service_type",
				"label": _("Parcel Service Type"),
				"fieldtype": "Data",  # needs to be "Data" for backward compat
				"options": "Parcel Service Type",
				"read_only": 1,
				"insert_after": "parcel_service",
			},
			{
				"fieldname": "shipping_col_break",
				"fieldtype": "Column Break",
				"insert_after": "parcel_service_type",
			},
			{
				"fieldname": "tracking_number",
				"label": _("Tracking Number"),
				"fieldtype": "Data",
				"read_only": 1,
				"translatable": 0,
				"insert_after": "shipping_col_break",
			},
			{
				"fieldname": "tracking_url",
				"label": _("Tracking URL"),
				"fieldtype": "Small Text",
				"read_only": 1,
				"translatable": 0,
				"insert_after": "tracking_number",
			},
			{
				"fieldname": "tracking_status",
				"label": _("Tracking Status"),
				"fieldtype": "Data",
				"read_only": 1,
				"translatable": 0,
				"insert_after": "tracking_url",
			},
			{
				"fieldname": "tracking_status_info",
				"label": _("Tracking Status Information"),
				"fieldtype": "Data",
				"read_only": 1,
				"translatable": 0,
				"insert_after": "tracking_status",
			},
		]
	}


def get_fields_for_patch(doctype: str, fieldnames: list[str]) -> dict[str, list[dict]]:
	"""Return specific fields that are needed for a patch."""
	return {doctype: [field for field in get_custom_fields()[doctype] if field["fieldname"] in fieldnames]}
