"""Opt-in, GET-only integration against the owned ERPNext laboratory.

No ERPAdapter, ToolExecutor, Tool.invoke or rule is mocked. No model is called.
The generated report records actual rules-only diagnoses, not model accuracy.
Run with ERP_SUPPORT_RUN_LIVE=1 and ERP_SUPPORT_LIVE_PROJECT=<owned lab project>.
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from holmes.core.docker_diagnosis import EvidenceContextLLM
from holmes.core.models import ToolCallResult
from holmes.core.tools import StructuredToolResultStatus, ToolInvokeContext
from holmes.core.tools_utils.tool_executor import ToolExecutor
from holmes.erpnext_support.engine import diagnose
from holmes.erpnext_support.security import safe_path
from holmes.erpnext_support.settings import Settings
from holmes.plugins.toolsets.erpnext_support import ERPAccessError, ERPAdapter, ERPConfig, ERPNextSupportToolset
from holmes.plugins.toolsets.erpnext_support.rules import build_report


LAB_ROOT = Path(__file__).resolve().parents[2] / "examples" / "erpnext-support" / "lab"
ERP_URL = "http://127.0.0.1:8090"


@dataclass
class LiveERP:
    runtime: Path
    config: ERPConfig
    seed: dict
    observations: list[dict] = field(default_factory=list, repr=False)

    def add(self, scenario: str, value: dict) -> None:
        self.observations.append({"scenario": scenario, "observed_at": datetime.now(timezone.utc).isoformat(),
                                  "result": self.config.sanitize(value)})


@pytest.fixture(scope="module")
def live_erp():
    if os.environ.get("ERP_SUPPORT_RUN_LIVE") != "1":
        pytest.skip("Real ERP laboratory tests require explicit opt-in; no live test has run.")
    project = os.environ.get("ERP_SUPPORT_LIVE_PROJECT", "")
    if not re.fullmatch(r"holmes-erp-[a-f0-9]{12}", project):
        pytest.fail("Explicit owned ERP laboratory project is required.")
    runtime_root = LAB_ROOT / ".runtime"
    runtime = safe_path(runtime_root, runtime_root / project)
    ownership = json.loads(safe_path(runtime, runtime / "ownership.json").read_text(encoding="utf-8"))
    connection = json.loads(safe_path(runtime, runtime / "connection.json").read_text(encoding="utf-8"))
    assert ownership.get("owned") is True and ownership.get("closed") is False
    assert connection["project"] == project and connection["base_url"] == ERP_URL
    assert connection["erpnext_version"] == "15.85.1" and connection["frappe_version"] == "15.87.0"
    credential_file = safe_path(runtime, runtime / "secret.json")
    assert Path(connection["credential_file"]) == credential_file
    credentials = json.loads(credential_file.read_text(encoding="utf-8"))
    config = ERPConfig(base_url=ERP_URL, **credentials, max_tool_timeout=15)
    seed = json.loads(safe_path(runtime, runtime / "seed-result.json").read_text(encoding="utf-8"))
    fixture = LiveERP(runtime, config, seed)
    yield fixture
    # Preserve each actual result without overwriting a previous run or secrets.
    report_directory = LAB_ROOT.parent / "reports"
    report_directory.mkdir(exist_ok=True)
    artifact = report_directory / ("erp-adapter-live-" + uuid.uuid4().hex + ".json")
    document = {"layer": "C: actual ERPNext API, original Tool.invoke and verified rules",
                "model": "not_called", "erpnext_version": "15.85.1", "frappe_version": "15.87.0",
                "project": project, "base_url": ERP_URL,
                "observations": fixture.observations,
                "note": "Actual observations only; pytest output supplies assertion outcomes. No model accuracy was measured."}
    with artifact.open("x", encoding="utf-8") as stream:
        json.dump(config.sanitize(document), stream, ensure_ascii=False, indent=2)
    print("Actual read-only ERP evidence artifact:", artifact)


def collect(live: LiveERP, scenario: str, config: ERPConfig | None = None, document: dict | None = None) -> dict:
    document = document or live.seed["documents"][scenario]
    toolset = ERPNextSupportToolset(config or live.config, document["name"],
                                  document["posting_date"] + "T" + document["posting_time"] + "+08:00",
                                  authorize=lambda: True)
    executor = ToolExecutor([toolset])
    context_llm = EvidenceContextLLM()
    calls: list[ToolCallResult] = []
    started = time.perf_counter()
    operations = [("erp_capabilities", {}), ("erp_delivery_context", {}),
                  ("erp_stock_evidence", {"line_name": document["line_name"]}),
                  ("erp_reservation_batch", {"line_name": document["line_name"]}),
                  ("erp_error_events", {}), ("erp_delivery_context", {})]
    try:
        for name, params in operations:
            identifier = "live_" + uuid.uuid4().hex
            context = ToolInvokeContext(llm=context_llm, max_token_count=8192,
                                        tool_call_id=identifier, tool_name=name, tool_number=len(calls) + 1)
            tool = executor.get_tool_by_name(name)
            assert tool is not None and not executor.ensure_toolset_initialized(name)
            result = tool.invoke(params, context)
            calls.append(ToolCallResult(tool_call_id=identifier, tool_name=name, toolset_name=toolset.name,
                                        description=name, result=result))
    finally:
        toolset.adapter.session.close()
    report = build_report(calls)
    success_count = sum(call.result.status == StructuredToolResultStatus.SUCCESS for call in calls)
    report["metrics"] = {"elapsed_seconds": time.perf_counter() - started, "tool_calls": len(calls),
                         "successful_calls": success_count, "tool_success_rate": success_count / len(calls)}
    report["mode"] = "rules_degraded"
    report["tool_calls"] = [call.model_dump(mode="json") for call in calls]
    # Both the primary and alternate diagnosis credentials must be scrubbed.
    live.add(scenario, (config or live.config).sanitize(report))
    return report


def stock_payload(report: dict) -> dict:
    return next(entry["payload"] for entry in report["evidence"] if entry["tool_name"] == "erp_stock_evidence")


def test_actual_versions_timezone_and_read_capabilities(live_erp):
    adapter = ERPAdapter(live_erp.config, authorize=lambda: True)
    adapter.begin_tool()
    try:
        envelope = adapter.capabilities()
    finally:
        adapter.session.close()
    live_erp.add("actual_capabilities", envelope)
    payload = envelope["payload"]
    assert payload["erpnext_version"] == "15.85.1" and payload["frappe_version"] == "15.87.0"
    assert payload["version_supported"] is True and payload["site_timezone"] == "Asia/Shanghai"
    assert payload["read_permissions"]["Delivery Note"] is True
    assert payload["operator_submit_permission"] == "unknown"
    assert payload["runtime_health"] == "not_configured"


@pytest.mark.parametrize("scenario,classification,status", [
    ("normal", "normal", "business_complete"),
    ("shortage", "stock_shortage", "manual_action"),
    ("historical", "historical_stock_shortage", "manual_action"),
    ("draft", "unable_to_confirm", "needs_information"),
])
def test_actual_seed_document_stock_and_rule_result(live_erp, scenario, classification, status):
    report = collect(live_erp, scenario)
    assert report["classification"] == classification
    assert report["status"] == status
    assert report["metrics"]["successful_calls"] == 6
    assert all(entry["collected_at"] for entry in report["evidence"])
    assert all(entry["tool_call_id"].startswith("live_") for entry in report["evidence"])
    payload = stock_payload(report)
    assert payload["authority"] == "erpnext.stock.utils.get_stock_balance"
    assert payload["snapshot_consistent"] is True
    if scenario == "shortage":
        assert payload["current_qty"] == 0 and live_erp.seed["actual_submit_errors"]["shortage"] == "NegativeStockError"
    if scenario == "historical":
        assert payload["posting_qty"] == 0 and payload["current_qty"] == 10
        assert live_erp.seed["actual_submit_errors"]["historical"] == "NegativeStockError"
    if scenario == "normal":
        document = live_erp.seed["documents"][scenario]
        assert any(row.get("voucher_no") == document["name"] and row.get("voucher_detail_no") == document["line_name"]
                   and row.get("is_cancelled") == 0 for row in payload["ledger"])
    assert any("运行层" in limit for limit in report["limits"])


def test_actual_user_permission_rejects_restricted_warehouse(live_erp):
    adapter = ERPAdapter(live_erp.config, authorize=lambda: True)
    adapter.begin_tool()
    started = time.perf_counter()
    try:
        with pytest.raises(ERPAccessError) as failure:
            adapter._document("Warehouse", live_erp.seed["restricted_warehouse"])
        live_erp.add("actual_restricted_warehouse_denied", {"failure_code": failure.value.code,
                                                          "elapsed_seconds": time.perf_counter() - started})
        assert failure.value.code == "permission_denied"
    finally:
        adapter.session.close()


def test_actual_cross_document_child_cannot_be_used_by_bound_tool(live_erp):
    normal = live_erp.seed["documents"]["normal"]
    foreign_line = live_erp.seed["documents"]["historical"]["line_name"]
    toolset = ERPNextSupportToolset(live_erp.config, normal["name"], "2026-10-05T10:00:00+08:00", authorize=lambda: True)
    executor = ToolExecutor([toolset])
    try:
        tool = executor.get_tool_by_name("erp_stock_evidence")
        context = ToolInvokeContext(llm=EvidenceContextLLM(), max_token_count=8192, tool_call_id="live_foreign_line",
                                    tool_name="erp_stock_evidence", tool_number=1)
        result = tool.invoke({"line_name": foreign_line}, context)
        live_erp.add("actual_cross_document_child_denied", result.model_dump(mode="json"))
        assert result.status == StructuredToolResultStatus.ERROR
        assert result.error == "line_outside_delivery_note"
        assert "current_qty" not in json.dumps(result.data)
    finally:
        toolset.adapter.session.close()


@pytest.mark.parametrize("scenario,classification,complete", [("shortage", "stock_shortage", False), ("normal", "normal", True)])
def test_actual_engine_no_model_path_is_explicitly_degraded(live_erp, tmp_path, scenario, classification, complete):
    document = live_erp.seed["documents"][scenario]
    settings = Settings(runtime=tmp_path, allowed_erp_origins=[ERP_URL])
    environment = {"base_url": ERP_URL, "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "model_consent": False}
    ticket = {"delivery_note": document["name"], "occurred_at": "2026-10-05T10:00:00+08:00", "description": "实际实验室缺货测试"}
    credentials = {"api_key": live_erp.config.api_key.get_secret_value(), "api_secret": live_erp.config.api_secret.get_secret_value()}
    steps: list[dict] = []
    report = diagnose(settings, environment, ticket, credentials, lambda: True, steps.append, [])
    live_erp.add("actual_engine_no_model_" + scenario, report)
    assert report["mode"] == "rules_degraded" and report["model_usage"]["status"] == "not_called"
    assert report["classification"] == classification
    assert report["business_state"]["business_complete"] is complete
    assert len(steps) == report["metrics"]["tool_calls"] == 6
    assert all(secret not in json.dumps(report) for secret in credentials.values())


def alternate_config(live: LiveERP, filename: str) -> ERPConfig:
    assert filename in ("limited-secret.json", "denied-secret.json")
    credential_file = safe_path(live.runtime, live.runtime / filename)
    if not credential_file.is_file():
        pytest.fail("Explicit alternate laboratory diagnosis credential file is missing.")
    values = json.loads(credential_file.read_text(encoding="utf-8"))
    return ERPConfig(base_url=ERP_URL, **values, max_tool_timeout=15)


def recheck_fixture(live: LiveERP) -> dict[str, Any]:
    return json.loads(safe_path(live.runtime, live.runtime / "recheck-result.json").read_text(encoding="utf-8"))


def test_actual_missing_error_log_permission_keeps_business_diagnosis(live_erp):
    config = alternate_config(live_erp, "limited-secret.json")
    report = collect(live_erp, "shortage_missing_error_log_permission", config, live_erp.seed["documents"]["shortage"])
    error_evidence = next(entry for entry in report["evidence"] if entry["tool_name"] == "erp_error_events")
    assert error_evidence["status"] == "error"
    assert error_evidence["payload"]["failure"]["code"] == "permission_denied"
    assert stock_payload(report)["current_qty"] == 0
    assert report["classification"] == "stock_shortage"
    assert report["metrics"]["successful_calls"] == 5
    assert any("Error Log" in message or "erp_error_events" in message for message in report["missing"])
    assert all(secret not in json.dumps(report) for secret in (config.api_key.get_secret_value(), config.api_secret.get_secret_value()))


def test_actual_denied_business_read_is_permission_result_not_stock_cause(live_erp):
    config = alternate_config(live_erp, "denied-secret.json")
    report = collect(live_erp, "shortage_denied_business_read", config, live_erp.seed["documents"]["shortage"])
    assert report["classification"] == "diagnostic_permission"
    assert not any(cause["code"] == "stock_shortage" for cause in report["causes"])
    contexts = [entry for entry in report["evidence"] if entry["tool_name"] == "erp_delivery_context"]
    assert contexts and all(entry["status"] == "error" and entry["payload"]["failure"]["code"] == "permission_denied" for entry in contexts)
    assert any("提交权限" in message for message in report["missing"])
    assert report["metrics"]["successful_calls"] == 1


def test_actual_restricted_delivery_note_does_not_leak_stock(live_erp):
    fixture = recheck_fixture(live_erp)
    document = {"name": fixture["restricted_delivery_note"], "line_name": fixture["restricted_line_name"],
                "posting_date": "2026-10-05", "posting_time": "10:00:00"}
    report = collect(live_erp, "restricted_delivery_note", document=document)
    assert report["classification"] == "diagnostic_permission"
    assert not any(entry["payload"].get("current_qty") is not None for entry in report["evidence"])
    assert any(entry["tool_name"] == "erp_delivery_context" and entry["status"] == "error" for entry in report["evidence"])


def test_actual_manual_handling_is_rechecked_by_fresh_reads_and_exact_ledger(live_erp):
    fixture = recheck_fixture(live_erp)
    document = {"name": fixture["delivery_note"], "line_name": fixture["line_name"],
                "posting_date": "2026-10-05", "posting_time": "10:00:00"}
    assert fixture["diagnosis_agent_writes"] is False
    assert fixture["before"]["actual_submit_error"] == "NegativeStockError"
    assert fixture["manual_actions"]  # Fixture setup/handling uses Administrator, outside the Agent.
    report = collect(live_erp, "manual_handling_recheck", document=document)
    assert report["status"] == "business_complete" and report["classification"] == "normal"
    assert stock_payload(report)["current_qty"] == 2
    assert any(row.get("voucher_no") == document["name"] and row.get("voucher_detail_no") == document["line_name"]
               and row.get("is_cancelled") == 0 for row in stock_payload(report)["ledger"])
    assert any("不能证明旧候选原因" in message for message in report["verification"])
    assert any("人工确认关闭" in message for message in report["verification"])
