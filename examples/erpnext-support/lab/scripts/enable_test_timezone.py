"""Fixture-only explicit settings read permission for the test diagnosis role."""

import os

import frappe
from frappe.permissions import add_permission, update_permission_property

os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site="frontend", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")
add_permission("System Settings", "Holmes Diagnosis Readonly", 0)
update_permission_property("System Settings", "Holmes Diagnosis Readonly", 0, "read", 1)
frappe.db.commit()
frappe.destroy()
print("Explicit test-only System Settings read permission enabled")
