"""Permission and native authority contract tests without a real ERP runtime."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def extension(monkeypatch):
    calls = []
    permissions = {}
    user_permissions = {"Company": [], "Warehouse": []}
    hooks = {}
    frappe = ModuleType("frappe")
    frappe.__version__ = "15.87.0"
    frappe.session = SimpleNamespace(user="probe@lab.invalid")
    frappe.PermissionError = PermissionError
    frappe.ValidationError = ValueError
    frappe.whitelist = lambda methods: (lambda function: function)
    def throw(message, exception):
        raise exception(message)
    frappe.throw = throw
    frappe.has_permission = lambda doctype, kind: permissions.get(doctype, True)
    frappe.get_list = lambda *args, **kwargs: []
    frappe.get_installed_apps = lambda: ["frappe", "erpnext", "holmes_readonly"]
    frappe.get_hooks = lambda name, default: hooks.get(name, default)
    frappe.get_module = lambda name: SimpleNamespace(__version__="0.1.0")
    frappe.db = SimpleNamespace(get_single_value=lambda *args: "Asia/Shanghai",
                                get_value=lambda *args: "2026-10-05 08:00:00")
    line = SimpleNamespace(name="line-1", item_code="LAB", warehouse="Dispatch - HL", get=lambda key: None)
    document = SimpleNamespace(items=[line], company="Holmes Isolated Lab",
                               modified="2026-10-05 08:00:00", posting_date="2026-10-04", posting_time="10:00:00")
    def doc(doctype, name):
        def check_permission(kind):
            calls.append(("permission", doctype, name, kind))
            if not permissions.get(doctype, True):
                raise PermissionError("fixture denied")
        result = document if doctype == "Delivery Note" else SimpleNamespace(has_batch_no=False, has_serial_no=False)
        result.check_permission = check_permission
        return result
    frappe.get_doc = doc
    erpnext = ModuleType("erpnext")
    erpnext.__version__ = "15.85.1"
    stock = ModuleType("erpnext.stock.utils")
    def balance(*args):
        calls.append(("balance", *args))
        return 0 if len(args) == 4 else 10
    stock.get_stock_balance = balance
    utils = ModuleType("frappe.utils")
    utils.now_datetime = lambda: "2026-10-05 12:00:00"
    perms = ModuleType("frappe.permissions")
    def get_user_permissions(user):
        calls.append(("user_permissions", user))
        return user_permissions
    perms.get_user_permissions = get_user_permissions
    for name, module in {"frappe": frappe, "erpnext": erpnext,
                         "erpnext.stock.utils": stock, "frappe.utils": utils,
                         "frappe.permissions": perms}.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).parents[1] / "frappe_app" / "holmes_readonly" / "api.py"
    spec = importlib.util.spec_from_file_location("lab_readonly_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(api=module, frappe=frappe, calls=calls, permissions=permissions,
                           user_permissions=user_permissions, hooks=hooks, line=line, document=document)


def test_stock_authority_uses_exact_native_date_time_and_current(extension):
    result = extension.api.stock_evidence("MAT-DN-00001", "line-1")
    assert result["posting_qty"] == 0 and result["current_qty"] == 10
    assert ("balance", "LAB", "Dispatch - HL", "2026-10-04", "10:00:00") in extension.calls
    assert ("balance", "LAB", "Dispatch - HL") in extension.calls
    assert ("user_permissions", "probe@lab.invalid") in extension.calls
    assert {row[1] for row in extension.calls if row[0] == "permission"} == {"Delivery Note", "Item", "Warehouse", "Company"}


@pytest.mark.parametrize("doctype", ["Delivery Note", "Item", "Warehouse", "Company", "Stock Ledger Entry"])
def test_missing_document_or_ledger_read_fails_before_aggregation(extension, doctype):
    extension.permissions[doctype] = False
    with pytest.raises(PermissionError):
        extension.api.stock_evidence("MAT-DN-00001", "line-1")
    assert not any(row[0] == "balance" for row in extension.calls)


def test_guest_denied(extension):
    extension.frappe.session.user = "Guest"
    with pytest.raises(PermissionError):
        extension.api.capabilities()


@pytest.mark.parametrize("name", ["../DN", "DN;SQL", "", "x" * 141, None])
def test_invalid_identifiers_rejected_before_loading_document(extension, name):
    with pytest.raises(ValueError):
        extension.api.stock_evidence(name, "line-1")
    assert not extension.calls


def test_line_must_belong_to_requested_document(extension):
    with pytest.raises(ValueError, match="belong"):
        extension.api.stock_evidence("MAT-DN-00001", "forged-line")
    assert not any(row[0] == "balance" for row in extension.calls)


@pytest.mark.parametrize("doctype", ["Batch", "Item", "Customer"])
def test_unadapted_user_permission_dimension_fails_closed(extension, doctype):
    extension.user_permissions[doctype] = []
    with pytest.raises(PermissionError, match="dimensions"):
        extension.api.stock_evidence("MAT-DN-00001", "line-1")
    assert not any(row[0] == "balance" for row in extension.calls)


@pytest.mark.parametrize("hook", ["permission_query_conditions", "has_permission"])
def test_custom_stock_ledger_hook_fails_closed(extension, hook):
    extension.hooks[hook] = {"Stock Ledger Entry": ["customer.custom_permission"]}
    with pytest.raises(PermissionError, match="not adapted"):
        extension.api.stock_evidence("MAT-DN-00001", "line-1")


def test_missing_timezone_permission_returns_unknown_without_reading_setting(extension):
    extension.permissions["System Settings"] = False
    extension.frappe.db.get_single_value = lambda *args: pytest.fail("setting read without permission")
    result = extension.api.capabilities()
    assert result["site_timezone"] is None
    assert "missing_read_permission:System Settings" in result["limitations"]
    assert "site_timezone_unverified" in result["limitations"]
    assert "runtime_health_not_available" in result["limitations"]


def test_snapshot_change_explicitly_limits_evidence(extension):
    extension.frappe.db.get_value = lambda *args: "changed"
    result = extension.api.stock_evidence("MAT-DN-00001", "line-1")
    assert "document_changed_during_collection" in result["limitations"]
