"""Deterministic rule scenarios using fabricated evidence contracts, not live ERP."""

from copy import deepcopy

import pytest

from holmes.plugins.toolsets.erpnext_support.rules import build_report
from holmes.plugins.toolsets.erpnext_support.models import ERPConfig, sanitize


def record(name, payload, status="success", limitations=None):
    return {"tool_call_id": f"call-{name}", "tool_name": name, "result": {"status": status, "data": {"source": "contract fixture", "query": {}, "collected_at": "2026-10-05T05:00:00+00:00", "business_at": "2026-10-05T10:00:00", "payload": payload, "limitations": limitations or []}, "elapsed_seconds": 0.01}}


def evidence(current=10, historical=10, **stock_updates):
    line = {"name": "line-1", "item_code": "ITEM-1", "warehouse": "WH-1", "stock_qty": 5}
    note = {"name": "DN-1", "docstatus": 0, "status": "Draft", "company": "Test Company", "modified": "2026-10-05 12:00:00", "posting_date": "2026-10-05", "posting_time": "10:00:00", "is_return": 0, "items": [line], "items_truncated": False}
    stock = {"delivery_note": "DN-1", "line_name": "line-1", "delivery_note_modified": note["modified"], "snapshot_consistent": True, "item_code": "ITEM-1", "warehouse": "WH-1", "required_stock_qty": 5, "posting_qty": historical, "current_qty": current, "authority": "erpnext.stock.utils.get_stock_balance", "allow_negative_stock": False, "item": {"name": "ITEM-1", "is_stock_item": 1, "disabled": 0, "has_batch_no": 0, "has_serial_no": 0, "allow_negative_stock": 0}, "warehouse_info": {"company": "Test Company", "disabled": 0, "is_group": 0}, "ledger": [], **stock_updates}
    return [record("erp_capabilities", {"erpnext_version": "15.85.1", "frappe_version": "15.87.0", "version_supported": True, "read_permissions": {"Delivery Note": True}}), record("erp_delivery_context", {"delivery_note": note, "sales_orders": []}), record("erp_stock_evidence", stock)]


def codes(report):
    return {value["code"] for value in report["causes"]}


@pytest.mark.parametrize("current,historical,expected", [(2, 2, "stock_shortage"), (10, 2, "historical_stock_shortage"), (5, 5, "unable_to_confirm")])
def test_current_and_historical_native_quantities_are_distinguished(current, historical, expected):
    report = build_report(evidence(current, historical))
    assert report["classification"] == expected
    assert all(value["status"] == "confirmed" for value in report["causes"])


def test_no_native_authority_means_no_stock_cause_even_when_ledger_fragment_is_negative():
    calls = evidence(0, 0, authority="manual_sum", ledger=[{"actual_qty": -999}])
    report = build_report(calls)
    assert not {"stock_shortage", "historical_stock_shortage"}.intersection(codes(report))
    assert any("权威" in value for value in report["missing"])


@pytest.mark.parametrize("limit", ["inventory_dimensions_not_adapted", "stock_reposting_in_progress", "custom_workflow_not_adapted", "missing_read_permission:Repost Item Valuation", "additional_apps_not_adapted"])
def test_unsupported_conditions_prevent_confirmation(limit):
    calls = evidence(0, 0)
    calls[2]["result"]["data"]["limitations"] = [limit]
    report = build_report(calls)
    assert "stock_shortage" not in codes(report)


def test_submitted_document_is_not_compared_to_post_transaction_balance():
    calls = evidence(0, 0)
    calls[1]["result"]["data"]["payload"]["delivery_note"]["docstatus"] = 1
    report = build_report(calls)
    assert "stock_shortage" not in codes(report)
    assert report["status"] != "business_complete"
    assert any("出库流水" in value for value in report["missing"])


def test_submitted_document_and_exact_uncancelled_voucher_line_verify_posting():
    calls = evidence(0, 0, ledger=[{"voucher_type": "Delivery Note", "voucher_no": "DN-1", "voucher_detail_no": "line-1", "actual_qty": -5, "is_cancelled": 0}])
    calls[1]["result"]["data"]["payload"]["delivery_note"]["docstatus"] = 1
    report = build_report(calls)
    assert report["status"] == "business_complete"
    assert report["classification"] == "normal"
    assert "人工" in report["verification"][-1]


def test_cancelled_voucher_line_does_not_prove_business_completion():
    calls = evidence(0, 0, ledger=[{"voucher_type": "Delivery Note", "voucher_no": "DN-1", "voucher_detail_no": "line-1", "actual_qty": -5, "is_cancelled": 1}])
    calls[1]["result"]["data"]["payload"]["delivery_note"]["docstatus"] = 1
    assert build_report(calls)["status"] != "business_complete"


def test_cancelled_note_has_document_state_problem():
    calls = evidence()
    calls[1]["result"]["data"]["payload"]["delivery_note"]["docstatus"] = 2
    assert "document_state" in codes(build_report(calls))


@pytest.mark.parametrize("status,docstatus", [("Closed", 1), ("On Hold", 1), ("Cancelled", 2), ("Draft", 0)])
def test_sales_order_preconditions_are_fixed_to_reviewed_version(status, docstatus):
    calls = evidence()
    calls[1]["result"]["data"]["payload"]["sales_orders"] = [{"name": "SO-1", "status": status, "docstatus": docstatus}]
    assert "document_precondition" in codes(build_report(calls))


def test_unknown_order_status_is_not_invented_as_invalid():
    calls = evidence()
    calls[1]["result"]["data"]["payload"]["sales_orders"] = [{"name": "SO-1"}]
    assert "document_precondition" not in codes(build_report(calls))


def test_two_reads_with_changed_modified_retract_quantity_conclusion():
    calls = evidence(0, 0)
    repeated = deepcopy(calls[1])
    repeated["result"]["data"]["payload"]["delivery_note"]["modified"] = "2026-10-05 12:00:01"
    calls.append(repeated)
    report = build_report(calls)
    assert report["evidence_inconsistent"] is True
    assert "stock_shortage" not in codes(report)


def test_stock_read_with_changed_document_disallows_same_snapshot():
    calls = evidence(0, 0, snapshot_consistent=False)
    assert build_report(calls)["evidence_inconsistent"] is True


def test_same_item_warehouse_multiple_lines_are_aggregated_in_stock_units():
    calls = evidence(8, 8)
    note = calls[1]["result"]["data"]["payload"]["delivery_note"]
    note["items"].append({**note["items"][0], "name": "line-2"})
    assert "stock_shortage" in codes(build_report(calls))


@pytest.mark.parametrize("identifiers", [("13800138000", "13900139000"), ("first@example.com", "second@example.com")])
def test_distinct_contact_shaped_skus_never_merge_into_false_quantity_shortage(identifiers):
    calls = evidence(8, 8)
    note = calls[1]["result"]["data"]["payload"]["delivery_note"]
    note["items"][0]["item_code"] = identifiers[0]
    note["items"].append({**note["items"][0], "name": "line-2", "item_code": identifiers[1]})
    first = calls[2]["result"]["data"]["payload"]
    first["item_code"] = first["item"]["name"] = identifiers[0]
    second = deepcopy(calls[2])
    second["tool_call_id"] = "call-second-stock"
    second["result"]["data"]["payload"]["line_name"] = "line-2"
    second["result"]["data"]["payload"]["item_code"] = identifiers[1]
    second["result"]["data"]["payload"]["item"]["name"] = identifiers[1]
    calls.append(second)
    config = ERPConfig(base_url="http://127.0.0.1:8090", api_key="private-key", api_secret="private-secret")
    calls = config.sanitize(calls)
    items = calls[1]["result"]["data"]["payload"]["delivery_note"]["items"]
    assert items[0]["item_code"] != items[1]["item_code"]
    report = build_report(calls)
    assert not {"stock_shortage", "historical_stock_shortage"}.intersection(codes(report))
    assert all("本单合计 10" not in value["text"] for value in report["facts"])


def test_legacy_generic_identifier_redaction_fails_closed_without_false_stock_cause():
    calls = evidence(8, 8)
    note = calls[1]["result"]["data"]["payload"]["delivery_note"]
    note["items"][0]["item_code"] = "13800138000"
    note["items"].append({**note["items"][0], "name": "line-2", "item_code": "13900139000"})
    stock = calls[2]["result"]["data"]["payload"]
    stock["item_code"] = stock["item"]["name"] = "13800138000"
    report = build_report(sanitize(calls))
    assert not {"stock_shortage", "historical_stock_shortage"}.intersection(codes(report))
    assert any("稳定别名" in item for item in report["missing"])


def test_unknown_site_timezone_does_not_confirm_historical_quantity_cause():
    calls = evidence(10, 0)
    calls[0]["result"]["data"]["payload"]["site_timezone"] = None
    calls[0]["result"]["data"]["limitations"] = ["ERP 站点时区未核验，历史时点与日志窗口无法确认。"]
    assert "historical_stock_shortage" not in codes(build_report(calls))


def test_truncated_lines_only_describe_visible_demand_and_never_confirm_whole_note_shortage():
    calls = evidence(0, 0)
    calls[1]["result"]["data"]["payload"]["delivery_note"]["items_truncated"] = True
    report = build_report(calls)
    shortages = [cause for cause in report["causes"] if cause["code"] == "stock_shortage"]
    assert shortages and all(cause["status"] == "candidate" for cause in shortages)
    assert any("已读取明细合计" in fact["text"] for fact in report["facts"])
    assert not any("本单合计" in fact["text"] for fact in report["facts"])
    assert any("完整发货单" in value for value in report["missing"])


def test_return_nonstock_and_serial_batch_do_not_reuse_normal_outbound_shortage_rule():
    for modifier in ("return", "nonstock", "batch", "serial"):
        calls = evidence(0, 0)
        if modifier == "return":
            calls[1]["result"]["data"]["payload"]["delivery_note"]["is_return"] = 1
        elif modifier == "nonstock":
            calls[2]["result"]["data"]["payload"]["item"]["is_stock_item"] = 0
        else:
            calls[2]["result"]["data"]["payload"]["item"][f"has_{modifier}_no"] = 1
        assert "stock_shortage" not in codes(build_report(calls))


def test_allowed_or_unknown_negative_stock_is_only_candidate_cause():
    for modifier in (True, None):
        calls = evidence(0, 0)
        if modifier is True:
            calls[2]["result"]["data"]["payload"]["allow_negative_stock"] = True
        else:
            calls[2]["result"]["data"]["payload"].pop("allow_negative_stock")
        report = build_report(calls)
        assert report["causes"][0]["status"] == "candidate"


def test_permission_failure_does_not_claim_fault_operator_can_submit():
    calls = [record("erp_delivery_context", {"failure": {"code": "permission_denied"}}, "error")]
    report = build_report(calls)
    assert "diagnostic_permission" in codes(report)
    assert any("故障操作用户" in value for value in report["missing"])


def test_read_error_remains_runtime_candidate_without_database_or_redis_root_cause():
    report = build_report([record("erp_capabilities", {"failure": {"code": "source_timeout"}}, "error")])
    assert report["causes"][0]["code"] == "runtime_unavailable"
    assert report["causes"][0]["status"] == "candidate"


def test_versions_flag_cannot_fake_a_verified_fixed_version_pair():
    calls = evidence(0, 0)
    calls[0]["result"]["data"]["payload"]["frappe_version"] = "15.92.0"
    assert "stock_shortage" not in codes(build_report(calls))


def test_readable_subset_reservation_warehouse_mismatch_is_only_candidate():
    calls = evidence()
    line = {"item_code": "ITEM-1", "warehouse": "WH-1", "against_sales_order": "SO-1", "so_detail": "so-line"}
    calls.append(record("erp_reservation_batch", {"line": line, "reservations": [{"docstatus": 1, "status": "Reserved", "warehouse": "WH-2"}]}))
    report = build_report(calls)
    finding = next(value for value in report["causes"] if value["code"] == "reservation_warehouse")
    assert finding["status"] == "candidate"


def test_batch_expiry_today_does_not_prove_expiry_at_historical_failure():
    calls = evidence()
    calls.append(record("erp_reservation_batch", {"line": {"item_code": "ITEM-1"}, "batches": [{"name": "BATCH-1", "item": "ITEM-1", "expiry_date": "2026-10-04", "disabled": 0}], "checked_on": "2026-10-05"}))
    report = build_report(calls)
    assert next(value for value in report["causes"] if value["code"] == "batch_expired_now")["status"] == "candidate"
    assert any("历史" in value["text"] for value in report["facts"])


def test_all_references_exist_and_tool_call_ids_remain_distinct_from_evidence_ids():
    report = build_report(evidence(0, 0))
    ids = {entry["id"] for entry in report["evidence"]}
    assert ids == {"E001", "E002", "E003"}
    assert all(set(value["evidence_ids"]).issubset(ids) for value in report["facts"] + report["causes"])
    assert report["evidence"][0]["tool_call_id"] == "call-erp_capabilities"


def test_missing_inventory_quantities_are_not_coerced_to_zero():
    report = build_report(evidence(current_qty=None, posting_qty=None))
    assert "stock_shortage" not in codes(report)
    assert any("不完整" in value for value in report["missing"])


def test_absent_warehouse_company_is_not_false_confirmed_company_conflict():
    report = build_report(evidence(warehouse_info={}))
    assert "document_precondition" not in codes(report)
