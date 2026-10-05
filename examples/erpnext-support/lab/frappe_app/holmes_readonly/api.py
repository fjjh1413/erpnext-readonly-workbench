"""New authenticated GET-only evidence endpoints; no ERP writes or permission bypass."""

import re

import erpnext
import frappe
from erpnext.stock.utils import get_stock_balance
from frappe.permissions import get_user_permissions
from frappe.utils import now_datetime

READ_TYPES = (
    "Delivery Note", "Sales Order", "Item", "Warehouse", "Bin", "Stock Ledger Entry",
    "Stock Reservation Entry", "Batch", "Serial and Batch Bundle", "Error Log",
    "Repost Item Valuation", "Inventory Dimension", "Workflow", "Stock Settings",
    "System Settings",
)


def authenticated() -> None:
    if frappe.session.user == "Guest":
        frappe.throw("Authentication required", frappe.PermissionError)


def configuration_limits() -> list[str]:
    limits = []
    for doctype, filters, label in (
        ("Inventory Dimension", {"disabled": 0}, "inventory_dimensions_not_adapted"),
        ("Workflow", {"is_active": 1, "document_type": ["in", ["Delivery Note", "Sales Order"]]}, "custom_workflow_not_adapted"),
        ("Repost Item Valuation", {"status": ["in", ["Queued", "In Progress"]]}, "stock_reposting_in_progress"),
    ):
        if not frappe.has_permission(doctype, "read"):
            limits.append(f"missing_read_permission:{doctype}")
        elif frappe.get_list(doctype, filters=filters, fields=["name"], limit_page_length=1):
            limits.append(label)
    if set(frappe.get_installed_apps()) - {"frappe", "erpnext", "holmes_readonly"}:
        limits.append("additional_apps_not_adapted")
    return limits


@frappe.whitelist(methods=["GET"])
def capabilities():
    authenticated()
    apps = []
    for name in frappe.get_installed_apps():
        module = frappe.get_module(name)
        apps.append({"name": name, "version": str(getattr(module, "__version__", "unknown"))})
    limits = configuration_limits() + ["operator_submit_permission_unknown", "runtime_health_not_available"]
    timezone = None
    if frappe.has_permission("System Settings", "read"):
        timezone = frappe.db.get_single_value("System Settings", "time_zone")
    else:
        limits.extend(["missing_read_permission:System Settings", "site_timezone_unverified"])
    return {"erpnext_version": erpnext.__version__, "frappe_version": frappe.__version__,
            "installed_apps": apps,
            "read_permissions": {name: bool(frappe.has_permission(name, "read")) for name in READ_TYPES},
            "site_timezone": timezone, "limitations": limits}


@frappe.whitelist(methods=["GET"])
def stock_evidence(delivery_note, line_name):
    authenticated()
    for value in (delivery_note, line_name):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,140}", value):
            frappe.throw("Invalid document identifier", frappe.ValidationError)
    document = frappe.get_doc("Delivery Note", delivery_note)
    document.check_permission("read")
    line = next((row for row in document.items if row.name == line_name), None)
    if line is None:
        frappe.throw("The line does not belong to the delivery note", frappe.ValidationError)
    frappe.get_doc("Item", line.item_code).check_permission("read")
    frappe.get_doc("Warehouse", line.warehouse).check_permission("read")
    if not frappe.has_permission("Stock Ledger Entry", "read"):
        frappe.throw("Stock Ledger Entry read permission required", frappe.PermissionError)
    if set(get_user_permissions(frappe.session.user)) - {"Company", "Warehouse"}:
        frappe.throw("Unverified user permission dimensions; stock aggregation is unavailable", frappe.PermissionError)
    for hook in ("permission_query_conditions", "has_permission"):
        if frappe.get_hooks(hook, {}).get("Stock Ledger Entry"):
            frappe.throw("Custom stock ledger permissions are not adapted", frappe.PermissionError)
    frappe.get_doc("Company", document.company).check_permission("read")
    item = frappe.get_doc("Item", line.item_code)
    limits = configuration_limits()
    if item.has_batch_no or item.has_serial_no or line.get("serial_and_batch_bundle"):
        limits.append("batch_or_serial_stock_not_adapted")
    modified = str(document.modified)
    posting_qty = get_stock_balance(line.item_code, line.warehouse,
                                   document.posting_date, document.posting_time)
    current_qty = get_stock_balance(line.item_code, line.warehouse)
    if str(frappe.db.get_value("Delivery Note", delivery_note, "modified")) != modified:
        limits.append("document_changed_during_collection")
    result = {"delivery_note": delivery_note, "line_name": line_name,
              "delivery_note_modified": modified, "item_code": line.item_code,
              "warehouse": line.warehouse, "posting_date": str(document.posting_date),
              "posting_time": str(document.posting_time), "posting_qty": float(posting_qty),
              "current_qty": float(current_qty), "current_as_of": str(now_datetime()),
              "authority": "erpnext.stock.utils.get_stock_balance", "limitations": limits}
    if frappe.has_permission("Stock Settings", "read"):
        result["allow_negative_stock"] = bool(frappe.db.get_single_value("Stock Settings", "allow_negative_stock"))
    return result
