"""Real read-only ERP API checks; isolated negative write-permission tests only."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from lab import ERPLab, ScopeError, safe_path, write_new


def verify(config_path):
    lab = ERPLab(config_path)
    lab.check()
    url = lab.url()
    if url != "http://127.0.0.1:8090":
        raise ScopeError("Verification requires this owned loopback lab")
    credential_path = lab.directory / "secret.json"
    safe_path(credential_path)
    credential = json.loads(credential_path.read_text())
    seed = json.loads((lab.directory / "seed-result.json").read_text())
    records = []
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = f"token {credential['api_key']}:{credential['api_secret']}"

    def call(name, method, path, statuses, params=None, payload=None, check=None, auth=True):
        started = time.perf_counter()
        headers = {} if auth else {"Authorization": ""}
        response = session.request(method, url + path, params=params, json=payload,
                                   headers=headers, timeout=15, allow_redirects=False)
        try:
            data = response.json()
        except ValueError:
            data = {}
        passed = response.status_code in statuses
        safe_result = None
        if passed and check:
            try:
                safe_result = check(data)
            except (AssertionError, KeyError, TypeError, ValueError):
                passed = False
        records.append({"case": name, "http_status": response.status_code, "passed": passed,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "result": safe_result})
        return data

    def capability_check(data):
        result = data["message"]
        assert result["erpnext_version"] == "15.85.1"
        assert result["frappe_version"] == "15.87.0"
        assert result["site_timezone"] == "Asia/Shanghai"
        assert result["read_permissions"]["Delivery Note"] is True
        return {key: result[key] for key in ("erpnext_version", "frappe_version", "site_timezone", "limitations")}

    call("actual_versions_and_permissions", "GET", "/api/method/holmes_readonly.api.capabilities", {200}, check=capability_check)
    call("guest_capabilities_denied", "GET", "/api/method/holmes_readonly.api.capabilities", {401, 403}, auth=False)
    call("non_get_extension_denied", "POST", "/api/method/holmes_readonly.api.capabilities", {403, 405})
    for scenario, document in seed["documents"].items():
        def document_check(data, expected=document):
            value = data["data"]
            assert value["name"] == expected["name"]
            assert value["docstatus"] == expected["docstatus"]
            return {"name": value["name"], "docstatus": value["docstatus"]}
        call(f"{scenario}_delivery_read", "GET", "/api/resource/Delivery%20Note/" + quote(document["name"], safe=""), {200}, check=document_check)
        expected_qty = (0.0, 10.0) if scenario == "historical" else (0.0, 0.0) if scenario == "shortage" else (18.0, 18.0)
        def stock_check(data, expected=expected_qty):
            value = data["message"]
            assert (value["posting_qty"], value["current_qty"]) == expected
            assert value["authority"] == "erpnext.stock.utils.get_stock_balance"
            return {key: value[key] for key in ("posting_qty", "current_qty", "posting_date", "posting_time", "authority", "limitations")}
        call(f"{scenario}_authoritative_stock", "GET", "/api/method/holmes_readonly.api.stock_evidence", {200},
             params={"delivery_note": document["name"], "line_name": document["line_name"]}, check=stock_check)
    normal = seed["documents"]["normal"]
    call("cross_document_child_denied", "GET", "/api/method/holmes_readonly.api.stock_evidence", {400, 417},
         params={"delivery_note": normal["name"], "line_name": seed["documents"]["shortage"]["line_name"]})
    call("restricted_warehouse_denied", "GET", "/api/resource/Warehouse/" + quote(seed["restricted_warehouse"], safe=""), {403})
    call("allowed_warehouse_read", "GET", "/api/resource/Warehouse/" + quote(seed["warehouse"], safe=""), {200})
    call("sales_order_read", "GET", "/api/resource/Sales%20Order/" + quote(seed["sales_orders"]["normal"], safe=""), {200})
    filters = json.dumps([["item_code", "=", normal["item_code"]], ["warehouse", "=", normal["warehouse"]]])
    call("stock_ledger_permission_checked_read", "GET", "/api/resource/Stock%20Ledger%20Entry", {200},
         params={"filters": filters, "fields": json.dumps(["name", "actual_qty", "qty_after_transaction"]), "limit_page_length": 10},
         check=lambda data: {"rows": len(data["data"])})
    call("bin_permission_checked_read", "GET", "/api/resource/Bin", {200},
         params={"filters": filters, "fields": json.dumps(["item_code", "warehouse", "actual_qty"]), "limit_page_length": 10},
         check=lambda data: {"rows": len(data["data"])})
    # Invalid attempt uses an existing lab document and must fail permissions
    # before any database change. This tester is never exposed as an Agent tool.
    call("readonly_delivery_write_denied", "PUT", "/api/resource/Delivery%20Note/" + quote(normal["name"], safe=""), {403},
         payload={"remarks": "Forbidden test-only write"})
    call("readonly_customer_create_denied", "POST", "/api/resource/Customer", {403},
         payload={"customer_name": "Forbidden test-only customer", "customer_type": "Company"})
    session.close()
    report = {"kind": "real_erp_api_permission_and_stock_verification", "model_invoked": False,
              "project": lab.project, "base_url": url, "utc": datetime.now(timezone.utc).isoformat(),
              "checks": records, "passed": sum(row["passed"] for row in records), "total": len(records),
              "elapsed_ms": round(sum(row["elapsed_ms"] for row in records), 3)}
    path = lab.directory / ("api-verification-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    write_new(path, report)
    print(json.dumps({"report": str(path), "passed": report["passed"], "total": report["total"], "checks": records}, indent=2))
    return report["passed"] == report["total"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.config) else 1)
