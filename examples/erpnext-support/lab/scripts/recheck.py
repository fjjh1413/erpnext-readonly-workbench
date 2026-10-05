"""Explicit isolated human-action fixture; never an Agent tool or auto repair."""

import json
import os
import secrets
from datetime import timedelta
from pathlib import Path

import frappe
from erpnext.stock.utils import get_stock_balance
from frappe.permissions import add_permission
from frappe.utils import now_datetime


def insert(doctype, values):
    document = frappe.get_doc({"doctype": doctype, **values})
    document.insert()
    return document


def main():
    os.chdir("/home/frappe/frappe-bench/sites")
    frappe.init(site="frontend", sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    frappe.set_user("Administrator")
    directory = Path("frontend/private")
    marker = directory / "holmes_recheck_result.json"
    if marker.exists():
        raise RuntimeError("This manual-action fixture already ran; refusing replacement")
    seed = json.loads((directory / "holmes_seed_result.json").read_text())
    company, warehouse = seed["company"], seed["warehouse"]
    customer = frappe.get_doc("Delivery Note", seed["documents"]["normal"]["name"]).customer
    item = insert("Item", {"item_code": "LAB-RECHECK", "item_name": "Manual recheck fixture",
                           "item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1,
                           "item_defaults": [{"company": company, "default_warehouse": warehouse}]}).name
    posting = now_datetime()
    order = insert("Sales Order", {"company": company, "customer": customer, "currency": "USD",
                                   "transaction_date": str(posting.date()), "delivery_date": str(posting.date()),
                                   "selling_price_list": "Standard Selling",
                                   "items": [{"item_code": item, "qty": 3, "rate": 10, "warehouse": warehouse,
                                              "delivery_date": str(posting.date())}]})
    order.submit()
    delivery = insert("Delivery Note", {"company": company, "customer": customer, "currency": "USD",
                                         "selling_price_list": "Standard Selling", "set_posting_time": 1,
                                         "posting_date": str(posting.date()), "posting_time": str(posting.time()),
                                         "items": [{"item_code": item, "qty": 3, "rate": 10, "warehouse": warehouse,
                                                    "against_sales_order": order.name, "so_detail": order.items[0].name}]})
    frappe.db.commit()
    before = {"docstatus": delivery.docstatus,
              "posting_qty": float(get_stock_balance(item, warehouse, delivery.posting_date, delivery.posting_time)),
              "current_qty": float(get_stock_balance(item, warehouse))}
    frappe.db.savepoint("before_manual_fix")
    try:
        delivery.submit()
    except frappe.ValidationError as exc:
        before["actual_submit_error"] = type(exc).__name__
        frappe.db.rollback(save_point="before_manual_fix")
    else:
        raise RuntimeError("Expected the pre-action fixture to reject submission")
    # Administrator deliberately performs these actions in this fixture.
    # The diagnosis API has no stock-entry, save, or submit capability.
    receipt_time = now_datetime()
    receipt = insert("Stock Entry", {"stock_entry_type": "Material Receipt", "company": company,
                                     "set_posting_time": 1, "posting_date": str(receipt_time.date()),
                                     "posting_time": str(receipt_time.time()),
                                     "items": [{"item_code": item, "qty": 5, "t_warehouse": warehouse,
                                                "basic_rate": 5, "cost_center": frappe.get_doc("Company", company).cost_center}]})
    receipt.submit()
    after_receipt = float(get_stock_balance(item, warehouse))
    delivery = frappe.get_doc("Delivery Note", delivery.name)
    corrected_time = now_datetime()
    delivery.posting_date = str(corrected_time.date())
    delivery.posting_time = str(corrected_time.time())
    delivery.save()
    delivery.submit()
    frappe.db.commit()
    after = {"docstatus": delivery.docstatus,
             "posting_qty": float(get_stock_balance(item, warehouse, delivery.posting_date, delivery.posting_time)),
             "current_qty": float(get_stock_balance(item, warehouse))}
    if before["current_qty"] != 0 or after_receipt != 5 or after["current_qty"] != 2 or after["docstatus"] != 1:
        raise RuntimeError("Actual manual-action ERP state differs from the fixture expectation")
    restricted = insert("Delivery Note", {"company": company, "customer": customer, "currency": "USD",
                                           "selling_price_list": "Standard Selling",
                                           "items": [{"item_code": "LAB-NORMAL", "qty": 1, "rate": 10,
                                                      "warehouse": seed["restricted_warehouse"]}]})
    read_types = ["Delivery Note", "Sales Order", "Item", "Warehouse", "Bin", "Stock Ledger Entry",
                  "Stock Reservation Entry", "Batch", "Serial and Batch Bundle", "Repost Item Valuation",
                  "Inventory Dimension", "Workflow", "Stock Settings", "System Settings", "Company", "Customer"]
    paths = {}
    for kind, role, doctype_permissions in (
        ("limited", "Holmes Diagnosis Missing Log", read_types),
        ("denied", "Holmes Diagnosis No Business Read", ["Company"]),
    ):
        insert("Role", {"role_name": role, "desk_access": 0})
        for doctype in doctype_permissions:
            add_permission(doctype, role, 0)
        user = insert("User", {"email": f"{kind}@lab.invalid", "first_name": "Lab permission fixture",
                               "enabled": 1, "send_welcome_email": 0, "user_type": "System User",
                               "roles": [{"role": role}]})
        key, secret = secrets.token_hex(16), secrets.token_hex(32)
        user.api_key, user.api_secret = key, secret
        user.save()
        insert("User Permission", {"user": user.name, "allow": "Company", "for_value": company,
                                   "apply_to_all_doctypes": 1})
        insert("User Permission", {"user": user.name, "allow": "Warehouse", "for_value": warehouse,
                                   "apply_to_all_doctypes": 1})
        path = directory / f"holmes_{kind}_secret.json"
        with path.open("x") as stream:
            json.dump({"api_key": key, "api_secret": secret}, stream)
        paths[kind] = path.name
    frappe.db.commit()
    result = {"fixture_actor": "Administrator", "diagnosis_agent_writes": False,
              "delivery_note": delivery.name, "line_name": delivery.items[0].name,
              "item_code": item, "warehouse": warehouse, "sales_order": order.name,
              "before": before, "material_receipt": receipt.name, "after_receipt_qty": after_receipt,
              "manual_actions": ["submit_material_receipt_qty_5", "update_delivery_posting_time", "submit_delivery_note"],
              "after": after, "restricted_delivery_note": restricted.name,
              "restricted_line_name": restricted.items[0].name, "credential_files": paths}
    with marker.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result), flush=True)
    frappe.destroy()


if __name__ == "__main__":
    main()
