"""Downstream diagnostic interpretation tests; no external services or model."""

import json

import pytest
from pydantic import ValidationError

from holmes.core.docker_diagnosis import CHECKS, DiagnosisReport, build_report
from holmes.core.models import ToolCallResult
from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus


def observation(name, data, number, status=StructuredToolResultStatus.SUCCESS):
    return ToolCallResult(tool_call_id=f"e{number}", tool_name=name, description=name,
                          result=StructuredToolResult(status=status, data=data, elapsed_seconds=0.01))


def baseline():
    config = {"MYSQL_HOST": "mysql", "MYSQL_PORT": "3306", "REDIS_HOST": "redis",
              "REDIS_PORT": "6379", "APP_MODE": "test-lab"}
    calls = []
    for i, (name, params) in enumerate(CHECKS):
        service = params.get("service")
        if name == "docker_lab_health":
            data = {"service": service, "state": {"status": "running", "running": True,
                                                 "health": "healthy", "exit_code": 0}}
        elif name == "docker_lab_config":
            data = {"service": "api", "config": config.copy()}
        elif name.endswith("mysql") or name.endswith("redis"):
            data = {"service": name.split("_")[-1], "reachable": True,
                    "check": "SELECT 1" if name.endswith("mysql") else "PING"}
        else:
            data = {"service": service, "lines": ["service started"], "truncated": False}
        calls.append(observation(name, data, i))
    return calls


def test_healthy_observations_do_not_claim_business_health():
    report = build_report("holmes-lab-123456789abc", baseline(), 0.5)
    assert not report.candidate_causes
    assert "证据不足" in report.summary
    assert report.metrics.successful_calls == 9
    assert report.metrics.tool_success_rate == 1
    assert all(f.evidence_ids for f in report.confirmed_facts)
    assert report.metrics.elapsed_seconds == 0.5


def test_unavailable_database_has_two_independent_observations():
    calls = baseline()
    calls[1].result.data["state"].update(status="exited", running=False)
    calls[4].result.data["reachable"] = False
    report = build_report("holmes-lab-123456789abc", calls, 1)
    cause = next(c for c in report.candidate_causes if c.code == "database_unavailable")
    assert cause.confidence == "high"
    assert cause.evidence_ids == ["e1", "e4"]
    # Failed connectivity is a completed observation, not a failed tool call.
    assert report.metrics.tool_success_rate == 1
    assert "停止原因" in cause.statement


def test_cache_endpoint_error_despite_host_ping_success():
    calls = baseline()
    calls[3].result.data["config"]["REDIS_PORT"] = "6380"
    report = build_report("holmes-lab-123456789abc", calls, 1)
    assert [c.code for c in report.candidate_causes] == ["cache_connection_error"]
    assert "不能单独证明" in "".join(report.limitations)


def test_invalid_service_config_is_observed_not_assumed():
    calls = baseline()
    calls[3].result.data["config"]["APP_MODE"] = "invalid"
    report = build_report("holmes-lab-123456789abc", calls, 1)
    assert report.candidate_causes[0].code == "service_configuration_error"
    assert report.candidate_causes[0].evidence_ids == ["e3"]


def test_tool_failures_are_missing_information_not_facts():
    calls = baseline()
    calls[4].result = StructuredToolResult(status=StructuredToolResultStatus.ERROR,
                                          error="permission denied")
    report = build_report("holmes-lab-123456789abc", calls, 1)
    assert report.metrics.tool_success_rate == 8 / 9
    assert not any(e.id == "e4" for e in report.supporting_evidence)
    assert not any("e4" in f.evidence_ids for f in report.confirmed_facts)
    assert any("permission denied" in item for item in report.missing_information)


def test_missing_and_empty_evidence_do_not_generate_causes():
    report = build_report("holmes-lab-123456789abc", [], 0)
    assert not report.confirmed_facts
    assert not report.candidate_causes
    assert report.metrics.tool_success_rate == 0
    assert len(report.missing_information) >= 9


def test_reports_reject_fabricated_evidence_references():
    report = build_report("holmes-lab-123456789abc", baseline(), 1).model_dump()
    report["confirmed_facts"][0]["evidence_ids"] = ["fabricated"]
    with pytest.raises(ValidationError, match="missing or unsuccessful"):
        DiagnosisReport.model_validate(report)


def test_truncated_logs_explicitly_state_limit():
    calls = baseline()
    calls[-1].result.data["truncated"] = True
    report = build_report("holmes-lab-123456789abc", calls, 1)
    assert any("截断" in item for item in report.missing_information)


def test_upstream_stringified_result_data_is_supported():
    calls = baseline()
    for call in calls:
        call.result.data = json.dumps(call.result.data)
    report = build_report("holmes-lab-123456789abc", calls, 1)
    assert len(report.supporting_evidence) == 9
