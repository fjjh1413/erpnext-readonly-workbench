"""Complete a locally installed, owned lab after an interrupted fixture setup."""

import os
import runpy

import erpnext
import frappe
from frappe.installer import install_app

os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site="frontend", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")
if erpnext.__version__ != "15.85.1" or frappe.__version__ != "15.87.0":
    raise RuntimeError("This fixture requires the inspected exact official versions")
if "erpnext" not in frappe.get_installed_apps():
    raise RuntimeError("The ERP app must already be locally installed")
if "holmes_readonly" not in frappe.get_installed_apps():
    install_app("holmes_readonly")
frappe.destroy()
runpy.run_path("/opt/lab/seed.py", run_name="__main__")
