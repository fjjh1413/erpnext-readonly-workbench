"""Architecture integration with mock ERP contracts and a scripted local model.

These tests run the real upstream ToolCallingLLM.call_stream, ToolExecutor and
Tool.invoke. They do not contact a model provider or a real ERPNext deployment.
Only ERPAdapter's business reads and the LLM provider boundary are replaced.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pytest
from litellm import ModelResponse

from holmes.core.llm import ContextWindowUsage, LLM
from holmes.erpnext_support.engine import Budget, diagnose
from holmes.erpnext_support.settings import Settings
from holmes.plugins.toolsets.erpnext_support import ERPAdapter


ALLOWED_TOOLS = {
    "erp_capabilities", "erp_delivery_context", "erp_stock_evidence",
    "erp_reservation_batch", "erp_error_events",
}
CREDENTIALS = {"api_key": "fixture-key-only-123", "api_secret": "fixture-secret-only-456"}


def response(content: dict | None = None, calls: list[dict] | None = None) -> ModelResponse:
    message = {"role": "assistant", "content": json.dumps(content) if content is not None else None}
    if calls:
        message["tool_calls"] = calls
    return ModelResponse(
        model="scripted-local-mock",
        choices=[{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    )


def call(identifier: str, name: str, params: dict) -> dict:
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(params)}}


class ScriptedMockLLM(LLM):
    """A local deterministic completion provider; upstream execution is real."""

    def __init__(self, replies: list[Any]):
        self.model = "scripted-local-mock"
        self.replies = iter(replies)
        self.requests: list[dict] = []

    def get_context_window_size(self) -> int:
        return 128000

    def get_maximum_output_token(self) -> int:
        return 4096

    def count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> ContextWindowUsage:
        return ContextWindowUsage(total_tokens=max(1, len(json.dumps(messages)) // 4), tools_tokens=0,
                                  system_tokens=0, user_tokens=0, tools_to_call_tokens=0,
                                  assistant_tokens=0, other_tokens=0)

    def completion(self, **kwargs: Any) -> ModelResponse:
        self.requests.append(deepcopy(kwargs))
        reply = next(self.replies, RuntimeError("scripted provider exhausted"))
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def erp_contract(tmp_path, monkeypatch):
    """Fabricated ERP observations, including real collection timestamps."""
    state: dict[str, Any] = {"authorized": True, "reads": [], "revoke_on_stock": False}
    settings = Settings(runtime=tmp_path, allowed_erp_origins=["http://127.0.0.1:18080"])
    environment = {"base_url": "http://127.0.0.1:18080", "erpnext_version": "15.85.1",
                   "frappe_version": "15.87.0", "model_consent": True}
    ticket = {"delivery_note": "DN-1", "occurred_at": "2026-10-05T10:00:00+08:00",
              "description": "库存不足；来自契约测试的虚构工单"}
    line = {"name": "line-1", "item_code": "ITEM-1", "warehouse": "WH-1", "stock_qty": 5}
    note = {"name": "DN-1", "docstatus": 0, "status": "Draft", "company": "Test Company",
            "modified": "2026-10-05 12:00:00", "posting_date": "2026-10-05",
            "posting_time": "10:00:00", "is_return": 0, "items": [line], "items_truncated": False}

    def envelope(name: str, payload: dict) -> dict:
        return {"source": "mock ERP contract (not live ERP)", "query": {"operation": name},
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "business_at": "2026-10-05T10:00:00+08:00", "payload": deepcopy(payload), "limitations": []}

    def capabilities(_adapter):
        state["reads"].append(("capabilities",))
        return envelope("capabilities", {"erpnext_version": "15.85.1", "frappe_version": "15.87.0",
                                         "version_supported": True, "read_permissions": {"Delivery Note": True}})

    def context(_adapter, delivery_note):
        state["reads"].append(("delivery_context", delivery_note))
        return envelope("delivery_context", {"delivery_note": note, "sales_orders": []})

    def stock(_adapter, delivery_note, line_name):
        state["reads"].append(("stock_evidence", delivery_note, line_name))
        if state["revoke_on_stock"]:
            state["authorized"] = False
        return envelope("stock_evidence", {
            "delivery_note": delivery_note, "line_name": line_name,
            "delivery_note_modified": note["modified"], "snapshot_consistent": True,
            "item_code": "ITEM-1", "warehouse": "WH-1", "required_stock_qty": 5,
            "posting_qty": 2, "current_qty": 2, "allow_negative_stock": False,
            "authority": "erpnext.stock.utils.get_stock_balance",
            "item": {"name": "ITEM-1", "is_stock_item": 1, "disabled": 0, "has_batch_no": 0,
                     "has_serial_no": 0, "allow_negative_stock": 0},
            "warehouse_info": {"company": "Test Company", "disabled": 0, "is_group": 0}, "ledger": [],
        })

    def reservation(_adapter, delivery_note, line_name):
        state["reads"].append(("reservation_batch", delivery_note, line_name))
        return envelope("reservation_batch", {"delivery_note": delivery_note, "line": line,
                                                "reservations": [], "batches": [], "bundles": []})

    def errors(_adapter, delivery_note, occurred_at):
        state["reads"].append(("error_events", delivery_note))
        return envelope("error_events", {"delivery_note": delivery_note, "events": []})

    monkeypatch.setattr(ERPAdapter, "capabilities", capabilities)
    monkeypatch.setattr(ERPAdapter, "delivery_context", context)
    monkeypatch.setattr(ERPAdapter, "stock_evidence", stock)
    monkeypatch.setattr(ERPAdapter, "reservation_batch", reservation)
    monkeypatch.setattr(ERPAdapter, "error_events", errors)
    return settings, environment, ticket, state


def run(contract, llm: ScriptedMockLLM | None = None) -> tuple[dict, list[dict]]:
    settings, environment, ticket, state = contract
    steps: list[dict] = []
    report = diagnose(settings, environment, ticket, CREDENTIALS, lambda: state["authorized"], steps.append, [], llm)
    return report, steps


def model_records(report: dict) -> dict[str, dict]:
    return {record["tool_call_id"]: record for record in report["tool_calls"] if not record["tool_call_id"].startswith("tool_")}


def test_original_stream_selects_business_tool_and_preserves_actual_records(erp_contract):
    llm = ScriptedMockLLM([
        response(calls=[call("model-stock", "erp_stock_evidence", {"line_name": "line-1"})]),
        response({"causes": [{"code": "stock_candidate", "text": "采集库存量不足", "evidence_ids": ["model-stock"]}]}),
    ])
    report, steps = run(erp_contract, llm)
    assert report["mode"] == "holmes_agent"
    assert len(llm.requests) == 2
    assert {entry["function"]["name"] for entry in llm.requests[0]["tools"]} == ALLOWED_TOOLS
    assert llm.requests[0]["stream"] is False  # original call_stream streams iterations, not tokens
    assert not any("stock_evidence" == entry[0] for entry in erp_contract[3]["reads"][:3])
    recorded = model_records(report)["model-stock"]
    assert recorded["tool_name"] == "erp_stock_evidence" and recorded["result"]["status"] == "success"
    assert recorded["result"]["elapsed_seconds"] >= 0
    stock_evidence = next(e for e in report["evidence"] if e["tool_call_id"] == "model-stock")
    assert report["model_hypotheses"][0]["evidence_ids"] == [stock_evidence["id"]]
    assert "stock_shortage" in {entry["code"] for entry in report["causes"]}
    assert len(steps) == len(report["tool_calls"]) == report["metrics"]["tool_calls"]
    assert [step["evidence_id"] for step in steps] == [e["id"] for e in report["evidence"]]
    assert len([entry for entry in erp_contract[3]["reads"] if entry[0] == "stock_evidence"]) == 1
    assert any(entry[0] == "reservation_batch" for entry in erp_contract[3]["reads"])
    assert any(entry[0] == "error_events" for entry in erp_contract[3]["reads"])
    assert all(credential not in json.dumps(llm.requests, default=str) for credential in CREDENTIALS.values())
    assert "mock ERP contract" in json.dumps(report)


@pytest.mark.parametrize("name,params", [
    ("run_bash", {"command": "echo must-not-run"}),
    ("erp_stock_evidence", {"line_name": "line-1", "url": "http://other-environment"}),
    ("erp_stock_evidence", {"line_name": 1}),
    ("erp_error_events", {"delivery_note": "OTHER-DN", "sql": "SELECT 1"}),
])
def test_unknown_tools_and_untrusted_arguments_never_execute_backend(erp_contract, name, params):
    llm = ScriptedMockLLM([
        response(calls=[call("invalid-model-tool", name, params)]),
        response({"causes": [{"code": "invalid", "text": "不能作为事实", "evidence_ids": ["invalid-model-tool"]}]}),
    ])
    report, _steps = run(erp_contract, llm)
    assert model_records(report)["invalid-model-tool"]["result"]["status"] == "error"
    assert report["model_hypotheses"] == []
    assert any("证据引用" in message for message in report["missing"])
    # Mandatory collector may perform valid reads after the failed model call;
    # there must be exactly one stock read, bound to the original task and row.
    assert [entry for entry in erp_contract[3]["reads"] if entry[0] == "stock_evidence"] == [("stock_evidence", "DN-1", "line-1")]
    assert not any("OTHER-DN" in entry for entry in erp_contract[3]["reads"])


def test_fabricated_evidence_id_is_rejected_without_promoting_model_assertion(erp_contract):
    llm = ScriptedMockLLM([response({"causes": [{"code": "made_up", "text": "数据库损坏", "evidence_ids": ["E999"]}]})])
    report, _steps = run(erp_contract, llm)
    assert report["model_hypotheses"] == []
    assert any("证据引用" in message for message in report["missing"])
    assert not any("数据库损坏" in fact["text"] for fact in report["confirmed_facts"])


def test_later_provider_failure_retains_completed_tool_evidence_and_rule_verdict(erp_contract):
    llm = ScriptedMockLLM([
        response(calls=[call("stock-before-provider-failure", "erp_stock_evidence", {"line_name": "line-1"})]),
        RuntimeError("provider failed: " + CREDENTIALS["api_secret"]),
    ])
    report, steps = run(erp_contract, llm)
    assert len(llm.requests) == 2
    assert report["mode"] == "rules_degraded"
    assert model_records(report)["stock-before-provider-failure"]["result"]["status"] == "success"
    assert report["classification"] == "stock_shortage"
    assert any("模型分析未完成" in message for message in report["missing"])
    assert any(step["tool_call_id"] == "stock-before-provider-failure" for step in steps)
    assert CREDENTIALS["api_secret"] not in json.dumps(report)
    assert any(entry[0] == "reservation_batch" for entry in erp_contract[3]["reads"])


@pytest.mark.parametrize("consent,provide_model", [(False, True), (False, False), (True, False)])
def test_no_model_or_no_consent_performs_rules_only_without_provider_access(erp_contract, consent, provide_model):
    erp_contract[1]["model_consent"] = consent
    llm = ScriptedMockLLM([AssertionError("Model must never be called")])
    report, _steps = run(erp_contract, llm if provide_model else None)
    assert llm.requests == []
    assert report["mode"] == "rules_degraded"
    assert report["model_usage"]["status"] == "not_called"
    assert report["classification"] == "stock_shortage"
    assert any("未完成完整模型Agent诊断" in message for message in report["limits"])


def test_repeated_model_query_is_audited_and_does_not_repeat_backend_read(erp_contract):
    llm = ScriptedMockLLM([
        response(calls=[call("first-stock", "erp_stock_evidence", {"line_name": "line-1"})]),
        response(calls=[call("repeated-stock", "erp_stock_evidence", {"line_name": "line-1"})]),
        response({}),
    ])
    report, _steps = run(erp_contract, llm)
    records = model_records(report)
    assert records["first-stock"]["result"]["status"] == "success"
    assert records["repeated-stock"]["result"]["status"] == "error"
    # The original safeguard runs before the ERP budget wrapper on repetition.
    assert "exact same parameters" in records["repeated-stock"]["result"]["error"]
    assert len([entry for entry in erp_contract[3]["reads"] if entry[0] == "stock_evidence"]) == 1


def test_budget_refuses_new_queries_after_call_maximum_and_environment_revocation(erp_contract):
    settings, _environment, _ticket, state = erp_contract
    settings.max_tool_calls = 5
    budget = Budget(settings, lambda: state["authorized"])
    assert all(budget.claim("erp_stock_evidence", {"line_name": f"line-{index}"}) is None for index in range(5))
    assert "预算" in budget.claim("erp_error_events", {})
    assert budget.count == 5
    state["authorized"] = False
    assert budget.allowed() is False
    assert "撤销" in budget.claim("erp_capabilities", {})


def test_parallel_round_budget_preserves_every_dispatched_tool_record(erp_contract):
    erp_contract[0].max_tool_calls = 5  # capabilities + two context reads leave two slots
    llm = ScriptedMockLLM([
        response(calls=[
            call("parallel-stock", "erp_stock_evidence", {"line_name": "line-1"}),
            call("parallel-reservation", "erp_reservation_batch", {"line_name": "line-1"}),
            call("parallel-errors", "erp_error_events", {}),
        ]),
        AssertionError("Budget exhaustion must stop a further provider round"),
    ])
    report, steps = run(erp_contract, llm)
    assert len(llm.requests) == 1
    records = model_records(report)
    assert set(records) == {"parallel-stock", "parallel-reservation", "parallel-errors"}
    assert sum(record["result"]["status"] == "error" for record in records.values()) >= 1
    assert len(erp_contract[3]["reads"]) <= 5
    assert set(records).issubset({step["tool_call_id"] for step in steps})


def test_revocation_during_model_tool_retains_error_audit_and_stops_more_reads(erp_contract):
    erp_contract[3]["revoke_on_stock"] = True
    llm = ScriptedMockLLM([
        response(calls=[call("stock-revoked-during-read", "erp_stock_evidence", {"line_name": "line-1"})]),
        response({}),
    ])
    report, steps = run(erp_contract, llm)
    assert len(llm.requests) == 1
    assert erp_contract[3]["reads"][-1] == ("stock_evidence", "DN-1", "line-1")
    records = model_records(report)
    assert "stock-revoked-during-read" in records
    assert records["stock-revoked-during-read"]["result"]["status"] == "error"
    assert any(step["tool_call_id"] == "stock-revoked-during-read" for step in steps)
    assert report["mode"] == "rules_degraded"
    assert not any(e["payload"].get("current_qty") is not None for e in report["evidence"])
