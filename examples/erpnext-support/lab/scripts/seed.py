"""Fixture-only ERP writes. This script is never an Agent tool or customer migration."""

import json
import os
import secrets
from datetime import timedelta
from pathlib import Path

import erpnext
import frappe
from erpnext.stock.utils import get_stock_balance
from erpnext.setup.setup_wizard.operations.install_fixtures import install as install_standard_fixtures
from frappe.permissions import add_permission, update_permission_property
from frappe.utils import getdate, nowdate

SITE = "frontend"
COMPANY = "Holmes Isolated Lab"
ROLE = "Holmes Diagnosis Readonly"
USER = "diagnosis@lab.invalid"


def insert(doctype, values):
    document = frappe.get_doc({"doctype": doctype, **values})
    document.insert()
    return document


def main():
    os.chdir("/home/frappe/frappe-bench/sites")
    frappe.init(site=SITE, sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    frappe.set_user("Administrator")
    marker = Path("/home/frappe/frappe-bench/sites/frontend/private/holmes_seed_result.json")
    if marker.exists():
        raise RuntimeError("The lab was seeded already; refusing to replace its documents")
    install_standard_fixtures(country="United States")
    print("ERP standard fixtures installed", flush=True)
    if not frappe.db.exists("Price List", "Standard Selling"):
        insert("Price List", {"price_list_name": "Standard Selling", "enabled": 1,
                              "selling": 1, "currency": "USD"})
    today = getdate(nowdate())
    frappe.db.set_single_value("System Settings", "time_zone", "Asia/Shanghai")
    frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
    if not frappe.db.exists("Fiscal Year", str(today.year)):
        insert("Fiscal Year", {"year": str(today.year), "year_start_date": f"{today.year}-01-01",
                              "year_end_date": f"{today.year}-12-31"})
    company = insert("Company", {"company_name": COMPANY, "abbr": "HL", "default_currency": "USD",
                                 "country": "United States", "create_chart_of_accounts_based_on": "Standard Template",
                                 "chart_of_accounts": "Standard"})
    warehouse = insert("Warehouse", {"warehouse_name": "Dispatch", "company": company.name,
                                     "parent_warehouse": "All Warehouses - HL"}).name
    other = insert("Warehouse", {"warehouse_name": "Restricted", "company": company.name,
                                 "parent_warehouse": "All Warehouses - HL"}).name
    print("Isolated company and warehouses created", flush=True)
    if not frappe.db.exists("Customer Group", "Lab Customers"):
        insert("Customer Group", {"customer_group_name": "Lab Customers", "parent_customer_group": "All Customer Groups"})
    if not frappe.db.exists("Territory", "Lab Territory"):
        insert("Territory", {"territory_name": "Lab Territory", "parent_territory": "All Territories"})
    customer = insert("Customer", {"customer_name": "Lab Customer", "customer_type": "Company",
                                   "customer_group": "Lab Customers", "territory": "Lab Territory"}).name
    items = {}
    for kind in ("normal", "shortage", "historical"):
        items[kind] = insert("Item", {"item_code": f"LAB-{kind.upper()}", "item_name": f"Lab {kind}",
                                     "item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1,
                                     "item_defaults": [{"company": company.name, "default_warehouse": warehouse}]}).name
    receipts = {}
    for kind, qty, date in (("normal", 20, today - timedelta(days=2)),
                            ("historical", 10, today)):
        receipt = insert("Stock Entry", {"stock_entry_type": "Material Receipt", "company": company.name,
                                         "set_posting_time": 1, "posting_date": str(date), "posting_time": "09:00:00",
                                         "items": [{"item_code": items[kind], "qty": qty, "t_warehouse": warehouse,
                                                    "basic_rate": 5, "cost_center": company.cost_center}]})
        receipt.submit()
        receipts[kind] = receipt.name
    print("Fixture stock receipts submitted", flush=True)
    documents = {}
    orders = {}
    for scenario, item, qty, date in (
        ("normal", items["normal"], 2, today),
        ("shortage", items["shortage"], 3, today),
        ("historical", items["historical"], 2, today - timedelta(days=1)),
        ("draft", items["normal"], 1, today),
    ):
        order = insert("Sales Order", {"customer": customer, "company": company.name,
                                       "transaction_date": str(today - timedelta(days=3)),
                                       "delivery_date": str(today), "currency": "USD", "selling_price_list": "Standard Selling",
                                       "items": [{"item_code": item, "qty": qty, "rate": 10,
                                                  "warehouse": warehouse, "delivery_date": str(today)}]})
        order.submit()
        delivery = insert("Delivery Note", {"customer": customer, "company": company.name,
                                             "set_posting_time": 1, "posting_date": str(date), "posting_time": "10:00:00",
                                             "currency": "USD", "selling_price_list": "Standard Selling",
                                             "items": [{"item_code": item, "qty": qty, "rate": 10, "warehouse": warehouse,
                                                        "against_sales_order": order.name, "so_detail": order.items[0].name}]})
        if scenario == "normal":
            delivery.submit()
        documents[scenario] = {"name": delivery.name, "line_name": delivery.items[0].name,
                               "item_code": item, "warehouse": warehouse, "docstatus": delivery.docstatus,
                               "posting_date": str(delivery.posting_date), "posting_time": str(delivery.posting_time)}
        orders[scenario] = order.name
    print("Four sales orders and delivery cases created", flush=True)
    # A deliberate attempt to submit an insufficient-stock fixture is expected
    # to fail. Keep the draft and record the actual exception category only.
    errors = {}
    for scenario in ("shortage", "historical"):
        frappe.db.savepoint(f"before_{scenario}")
        try:
            frappe.get_doc("Delivery Note", documents[scenario]["name"]).submit()
        except frappe.ValidationError as exc:
            errors[scenario] = type(exc).__name__
            frappe.db.rollback(save_point=f"before_{scenario}")
        else:
            raise RuntimeError(f"Expected {scenario} submit to fail")
    insert("Role", {"role_name": ROLE, "desk_access": 0})
    read_types = ["Delivery Note", "Sales Order", "Item", "Warehouse", "Bin", "Stock Ledger Entry",
                  "Stock Reservation Entry", "Batch", "Serial and Batch Bundle", "Error Log",
                  "Repost Item Valuation", "Inventory Dimension", "Workflow", "Stock Settings",
                  "System Settings",
                  "Company", "Customer", "UOM", "Item Group", "Report"]
    for doctype in read_types:
        add_permission(doctype, ROLE, 0)
        for permission in ("read", "select", "report"):
            update_permission_property(doctype, ROLE, 0, permission, 1)
        for permission in ("create", "write", "delete", "submit", "cancel", "amend", "share", "export", "print", "email", "import"):
            update_permission_property(doctype, ROLE, 0, permission, 0)
    user = insert("User", {"email": USER, "first_name": "Lab Diagnosis", "enabled": 1,
                           "user_type": "System User", "send_welcome_email": 0,
                           "roles": [{"role": ROLE}]})
    key, secret = secrets.token_hex(16), secrets.token_hex(32)
    user.api_key = key
    user.api_secret = secret
    user.save()
    insert("User Permission", {"user": USER, "allow": "Company", "for_value": company.name,
                               "apply_to_all_doctypes": 1})
    insert("User Permission", {"user": USER, "allow": "Warehouse", "for_value": warehouse,
                               "apply_to_all_doctypes": 1})
    frappe.db.commit()
    result = {"site": SITE, "company": company.name, "warehouse": warehouse, "restricted_warehouse": other,
              "erpnext_version": erpnext.__version__, "frappe_version": frappe.__version__,
              "documents": documents, "sales_orders": orders, "receipts": receipts,
              "actual_submit_errors": errors,
              "balances": {kind: {"current": float(get_stock_balance(item, warehouse)),
                                   "at_yesterday_10": float(get_stock_balance(item, warehouse, str(today - timedelta(days=1)), "10:00:00"))}
                           for kind, item in items.items()}}
    marker.parent.mkdir(exist_ok=True)
    with marker.open("x") as stream:
        json.dump(result, stream, indent=2)
    with (marker.parent / "holmes_diagnosis_secret.json").open("x") as stream:
        json.dump({"api_key": key, "api_secret": secret}, stream)
    print(json.dumps({"seed_completed": True, "documents": documents, "actual_submit_errors": errors}))
    frappe.destroy()


if __name__ == "__main__":
    main()
