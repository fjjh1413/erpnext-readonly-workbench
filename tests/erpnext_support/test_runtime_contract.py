"""Independent security regressions using mock Docker/HTTP contracts only.

No Docker daemon, live ERP API, collector listener or model provider is contacted.
Original collector, RuntimeTool.invoke and Pydantic report validation run normally.
Actual ERP integration observations are in test_erp_live.py and its dated reports.
"""

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from holmes.core.docker_diagnosis import EvidenceContextLLM
from holmes.core.models import ToolCallResult
from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus, ToolInvokeContext
from holmes.erpnext_support import collector, runtime_tool
from holmes.erpnext_support.collector import LabCollector, create_collector
from holmes.erpnext_support.engine import Budget, BudgetTool
from holmes.erpnext_support.models import DiagnosticResult
from holmes.erpnext_support.runtime_tool import RuntimeTool
from holmes.erpnext_support.security import safe_path, sanitize
from holmes.erpnext_support.settings import Settings
from holmes.plugins.toolsets.docker_readonly.backend import CommandOutput
from holmes.plugins.toolsets.docker_readonly.security import redact
from holmes.plugins.toolsets.erpnext_support.rules import build_report
from holmes.plugins.toolsets.erpnext_support import ERPAdapter, ERPConfig, ERPNextSupportToolset


TOKEN = "mock-collector-token-" + "a" * 32
PROJECT = "holmes-erp-012345abcdef"
LAB_ID = "a" * 32
NETWORK = "f" * 64


def snapshot() -> dict:
    return {"source": "local-restricted-erp-collector", "query": {"project": PROJECT,
            "services": list(collector.SERVICES), "log_tail": 50, "log_window_seconds": 900},
            "collected_at": datetime.now(timezone.utc).isoformat(), "business_at": None,
            "payload": {"services": {name: {"category": category, "state": "running", "health": "not_configured",
                         "restart_count": 0, "ping": "PONG" if category == "redis" else "not_checked"}
                         for name, category in collector.SERVICES.items()},
                         "application_logs": "password=mock-log-secret user@example.com 13800138000 " + TOKEN},
            "limitations": ["Runtime observations alone do not prove Delivery Note completion."]}


class MockDockerRunner:
    """Semantic Docker responses; command argv is recorded, never executed."""

    def __init__(self):
        self.commands: list[list[str]] = []
        self.endpoint = "unix:///var/run/docker.sock"
        self.containers = [{"id": chr(97 + index) * 64,
                            "labels": {"com.docker.compose.project": PROJECT,
                                       "io.holmes.erp-lab": LAB_ID, "com.docker.compose.service": name},
                            "networks": {"owned": {"NetworkID": NETWORK}},
                            "state": "running", "running": True, "health": "not_configured", "restart_count": 0}
                           for index, name in enumerate(collector.SERVICES)]
        self.networks = [{"id": NETWORK, "labels": {"com.docker.compose.project": PROJECT, "io.holmes.erp-lab": LAB_ID},
                          "containers": {value["id"]: {} for value in self.containers}}]
        self.logs = "password=mock-root-password; email=user@example.com " + "x" * 13000

    def run(self, args: list[str], *, allow_truncation: bool = False) -> CommandOutput:
        self.commands.append(args)
        assert args[:2] == ["--context", "desktop-linux"]
        operation = args[2:]
        if operation[:2] == ["context", "inspect"]:
            return CommandOutput(json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}]))
        if operation[:2] == ["ps", "-aq"]:
            return CommandOutput("\n".join(value["id"] for value in self.containers))
        if operation[:2] == ["inspect", "--type"]:
            return CommandOutput("\n".join(json.dumps(value) for value in self.containers))
        if operation[:2] == ["network", "inspect"]:
            return CommandOutput("\n".join(json.dumps(value) for value in self.networks))
        if operation[0] == "exec":
            assert len(operation) == 4 and operation[2:] == ["redis-cli", "PING"]
            assert operation[1] in {value["id"] for value in self.containers
                                    if value["labels"]["com.docker.compose.service"].startswith("redis-")}
            return CommandOutput("PONG")
        if operation[0] == "logs":
            assert operation[:6] == ["logs", "--timestamps", "--tail", "50", "--since", "900s"]
            assert allow_truncation
            return CommandOutput(self.logs, truncated=True)
        raise AssertionError("Unexpected Docker command: " + operation[0])


@pytest.fixture
def collector_contract(tmp_path, monkeypatch):
    example = tmp_path / "example"
    directory = example / "lab" / ".runtime" / PROJECT
    (directory / "secrets").mkdir(parents=True)
    (directory / "secrets" / "db_root_password").write_text("mock-root-password")
    (directory / "secrets" / "admin_password").write_text("mock-admin-password")
    path = directory / "config.json"
    path.write_text(json.dumps({"project": PROJECT, "lab_id": LAB_ID, "context": "desktop-linux"}))
    runner = MockDockerRunner()
    owner = {"owned": True, "closed": False, "containers": [value["id"] for value in runner.containers], "networks": [NETWORK]}
    (directory / "ownership.json").write_text(json.dumps(owner))
    monkeypatch.setattr(collector, "EXAMPLE", example)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return LabCollector(path, runner=runner), runner


def test_collector_uses_only_fixed_read_operations_and_restricted_projection(collector_contract):
    value, runner = collector_contract
    data = value.snapshot()
    assert set(data["payload"]["services"]) == set(collector.SERVICES)
    assert len(data["payload"]["application_logs"]) <= 12000
    inspect = next(args for args in runner.commands if args[2:4] == ["inspect", "--type"])
    projection = inspect[inspect.index("--format") + 1]
    assert ".Env" not in projection and ".Cmd" not in projection and "Health.Log" not in projection
    assert not any(verb in args for args in runner.commands for verb in ("stop", "start", "restart", "kill", "rm", "run"))
    assert "mock-root-password" not in json.dumps(data)
    assert any("截断" in message for message in data["limitations"])


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://remote", "https://docker.example"])
def test_remote_transport_is_rejected_before_container_access(collector_contract, endpoint):
    value, runner = collector_contract
    runner.endpoint = endpoint
    with pytest.raises(ValueError, match="local"):
        value.snapshot()
    assert len(runner.commands) == 1


def test_docker_host_override_is_rejected_before_any_daemon_read(collector_contract, monkeypatch):
    value, runner = collector_contract
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    with pytest.raises(ValueError, match="Remote"):
        value.snapshot()
    assert runner.commands == []


@pytest.mark.parametrize("mutation", ["wrong_project", "wrong_lab", "foreign_network", "foreign_peer"])
def test_collector_denies_scope_changes_before_logs_or_exec(collector_contract, mutation):
    value, runner = collector_contract
    if mutation == "wrong_project":
        runner.containers[0]["labels"]["com.docker.compose.project"] = "other-project"
    elif mutation == "wrong_lab":
        runner.containers[0]["labels"]["io.holmes.erp-lab"] = "b" * 32
    elif mutation == "foreign_network":
        runner.containers[0]["networks"]["owned"]["NetworkID"] = "0" * 64
    else:
        runner.networks[0]["containers"]["0" * 64] = {}
    with pytest.raises(ValueError):
        value.snapshot()
    assert not any(args[2] in ("exec", "logs") for args in runner.commands)


def test_collector_http_token_query_and_method_boundaries(collector_contract):
    value, runner = collector_contract
    with TestClient(create_collector(value, TOKEN)) as client:
        assert client.get("/snapshot").status_code == 403
        assert client.get("/snapshot", headers={"Authorization": "Bearer wrong"}).status_code == 403
        assert client.get("/snapshot?command=restart", headers={"Authorization": "Bearer " + TOKEN}).status_code == 403
        assert client.post("/snapshot", headers={"Authorization": "Bearer " + TOKEN}).status_code == 405
        assert runner.commands == []
        response = client.get("/snapshot", headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200 and TOKEN not in response.text


@pytest.mark.parametrize("token", ["", "short", "x" * 300])
def test_collector_rejects_weak_or_unbounded_server_token(collector_contract, token):
    with pytest.raises(ValueError):
        create_collector(collector_contract[0], token)


def test_non_ascii_authorization_is_denied_without_server_error(collector_contract):
    with TestClient(create_collector(collector_contract[0], TOKEN), raise_server_exceptions=False) as client:
        result = client.get("/snapshot", headers=[(b"Authorization", b"Bearer \xff")])
    assert result.status_code == 403
    assert collector_contract[1].commands == []


class MockHTTPResponse:
    def __init__(self, chunks: list[bytes], status: int = 200, between_chunks=None):
        self.chunks, self.status_code, self.between_chunks = chunks, status, between_chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def iter_content(self, chunk_size: int):
        assert chunk_size in (1, 4096)
        for chunk in self.chunks:
            if self.between_chunks:
                self.between_chunks()
            for index in range(0, len(chunk), chunk_size):
                yield chunk[index:index + chunk_size]


@pytest.fixture
def http_contract(monkeypatch):
    state: dict[str, Any] = {"requests": [], "authorized": True,
                             "response": MockHTTPResponse([json.dumps(snapshot()).encode()])}

    class MockHTTPSession:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            assert self.trust_env is False
            state["requests"].append({"url": url, **kwargs})
            return state["response"]

    monkeypatch.setattr(runtime_tool.requests, "Session", MockHTTPSession)
    return state


def invoke_runtime(state: dict, services: list[str] | None = None, params: dict | None = None, timeout: float = 1):
    tool = RuntimeTool({"base_url": "http://127.0.0.1:8767", "token": TOKEN},
                       ["application", "database", "redis"] if services is None else services,
                       lambda: state["authorized"], timeout)
    context = ToolInvokeContext(llm=EvidenceContextLLM(), max_token_count=8192,
                                tool_call_id="contract-runtime", tool_name=tool.name, tool_number=1)
    return tool.invoke(params or {}, context), tool


def test_runtime_credentials_are_private_and_only_bound_endpoint_is_requested(http_contract):
    result, tool = invoke_runtime(http_contract)
    assert result.status == StructuredToolResultStatus.SUCCESS
    assert TOKEN not in tool.model_dump_json() + json.dumps(tool.get_openai_format()) + json.dumps(result.model_dump(mode="json"))
    request = http_contract["requests"][0]
    assert request["url"] == "http://127.0.0.1:8767/snapshot"
    assert request["allow_redirects"] is False and request["stream"] is True
    assert request["headers"] == {"Authorization": "Bearer " + TOKEN}


@pytest.mark.parametrize("params", [{"url": "http://other"}, {"service": "db"}, {"command": "restart"}, {"token": "secret"}])
def test_runtime_tool_rejects_untrusted_parameters_without_http(http_contract, params):
    result, _tool = invoke_runtime(http_contract, params=params)
    assert result.status == StructuredToolResultStatus.ERROR and result.error == "invalid_tool_parameters"
    assert http_contract["requests"] == []
    assert "secret" not in json.dumps(result.model_dump(mode="json"))


def test_runtime_scope_filters_payload_and_query_metadata(http_contract):
    result, _tool = invoke_runtime(http_contract, services=["application"])
    assert result.status == StructuredToolResultStatus.SUCCESS
    assert set(result.data["payload"]["services"]) == {"backend", "frontend"}
    assert set(result.data["query"]["services"]) == {"backend", "frontend"}


def test_runtime_unknown_payload_metadata_does_not_leave_client_boundary(http_contract):
    value = snapshot()
    value["payload"]["raw_inspect"] = {"host_private_field": "private-unapproved-value"}
    http_contract["response"] = MockHTTPResponse([json.dumps(value).encode()])
    result, _tool = invoke_runtime(http_contract)
    assert "private-unapproved-value" not in json.dumps(result.model_dump(mode="json"))


def test_runtime_authorization_revoked_before_and_during_read_is_fail_closed(http_contract):
    http_contract["authorized"] = False
    result, _tool = invoke_runtime(http_contract)
    assert result.error == "authorization_revoked" and http_contract["requests"] == []
    http_contract["authorized"] = True
    http_contract["response"] = MockHTTPResponse([json.dumps(snapshot()).encode()], between_chunks=lambda: http_contract.update(authorized=False))
    result, _tool = invoke_runtime(http_contract)
    assert result.status == StructuredToolResultStatus.ERROR and result.error == "authorization_revoked"
    assert result.data is None


@pytest.mark.parametrize("status", [302, 403, 503])
def test_runtime_http_failures_never_return_response_body(http_contract, status):
    http_contract["response"] = MockHTTPResponse([b"private-response-body"], status=status)
    result, _tool = invoke_runtime(http_contract)
    assert result.status == StructuredToolResultStatus.ERROR
    assert "private-response-body" not in json.dumps(result.model_dump(mode="json"))


def test_runtime_response_byte_limit_is_enforced(http_contract):
    http_contract["response"] = MockHTTPResponse([b"x" * 65537])
    result, _tool = invoke_runtime(http_contract)
    assert result.status == StructuredToolResultStatus.ERROR
    assert result.data is None


def test_runtime_http_transport_timeout_is_explicit_without_raw_secret(http_contract):
    def timeout(*_args, **_kwargs):
        raise runtime_tool.requests.Timeout("private transport=" + TOKEN)

    session = runtime_tool.requests.Session
    original = session.get
    session.get = timeout
    try:
        result, _tool = invoke_runtime(http_contract)
    finally:
        session.get = original
    assert result.status == StructuredToolResultStatus.ERROR and result.error == "tool_timeout"
    assert TOKEN not in result.model_dump_json()


def test_runtime_stream_has_one_total_tool_deadline_even_when_every_chunk_arrives_in_time(http_contract, monkeypatch):
    clock = {"seconds": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["seconds"])
    value = json.dumps(snapshot()).encode()
    chunks = [value[index:index + 100] for index in range(0, len(value), 100)]
    # Each simulated network gap is 0.4s < the 1s read timeout; aggregate exceeds it.
    http_contract["response"] = MockHTTPResponse(chunks, between_chunks=lambda: clock.update(seconds=clock["seconds"] + 0.4))
    result, _tool = invoke_runtime(http_contract, timeout=1)
    assert result.status == StructuredToolResultStatus.ERROR
    assert result.data is None


def diagnostic_contract() -> dict:
    # The real RuntimeTool scrubs data before constructing its ToolCallResult.
    envelope = sanitize(snapshot(), [TOKEN])
    call = ToolCallResult(tool_call_id="actual-contract-call", tool_name="erp_runtime_health", description="mock runtime contract",
                          result=StructuredToolResult(status=StructuredToolResultStatus.SUCCESS, data=envelope, params={}, elapsed_seconds=0.01))
    report = build_report([call])
    report.update(mode="rules_degraded", metrics={"tool_calls": 1}, tool_calls=[call.model_dump(mode="json")])
    report["facts"] = [{"text": "Collector returned the fixture service observation", "evidence_ids": ["E001"]}]
    return report


def test_diagnostic_schema_accepts_program_generated_lineage():
    assert DiagnosticResult.model_validate(diagnostic_contract()).evidence[0].tool_call_id == "actual-contract-call"


@pytest.mark.parametrize("field,value", [("tool_name", "run_bash"), ("status", "error"),
                                         ("payload", {"services": {"made_up": "forged"}}),
                                         ("source", "invented-production-source"),
                                         ("query", {"project": "other-customer"})])
def test_diagnostic_schema_rejects_evidence_changed_from_recorded_call(field, value):
    report = diagnostic_contract()
    report["evidence"][0][field] = value
    with pytest.raises(ValidationError):
        DiagnosticResult.model_validate(report)


def test_diagnostic_schema_rejects_fake_and_unsuccessful_hypothesis_references():
    report = diagnostic_contract()
    report["model_hypotheses"] = [{"code": "fake", "text": "unproven", "evidence_ids": ["E999"]}]
    with pytest.raises(ValidationError):
        DiagnosticResult.model_validate(report)
    report = diagnostic_contract()
    report["tool_calls"][0]["result"]["status"] = "error"
    report["evidence"][0]["status"] = "error"
    report["model_hypotheses"] = [{"code": "failed", "text": "unproven", "evidence_ids": ["E001"]}]
    with pytest.raises(ValidationError):
        DiagnosticResult.model_validate(report)


def test_collector_credential_alias_and_origin_remain_server_bound(tmp_path):
    settings = Settings(tmp_path, ["http://127.0.0.1:8090"])
    directory = tmp_path / "collectors"
    directory.mkdir()
    (directory / "lab.json").write_text(json.dumps({"base_url": "http://127.0.0.1:8767", "token": TOKEN}))
    assert settings.collector_credentials("lab")["base_url"] == "http://127.0.0.1:8767"
    with pytest.raises(ValueError):
        settings.collector_credentials("../lab")
    (directory / "remote.json").write_text(json.dumps({"base_url": "http://remote:8767", "token": TOKEN}))
    with pytest.raises(ValueError):
        settings.collector_credentials("remote")
    with pytest.raises(ValueError):
        safe_path(tmp_path, tmp_path / ".." / "other" / "secret.json")


def test_contact_redaction_preserves_call_identifiers_and_business_clock():
    value = "phone 13800138000 email user@example.com id=call_a13800138000b time=2026-10-05 23:52:08.335059 token=" + TOKEN
    result = sanitize(value, [TOKEN])
    assert "call_a13800138000b" in result and "2026-10-05 23:52:08.335059" in result
    assert "phone 13800138000" not in result and "user@example.com" not in result and TOKEN not in result


def test_secret_redaction_does_not_rescan_long_words_or_repeated_keyword_labels():
    values = ["x" * 12000, "password" * 1500, "密码" * 6000]
    started = time.perf_counter()
    for value in values:
        assert redact(value) == value
        assert sanitize({"payload": {"logs": value}})["payload"]["logs"] == value
    assert time.perf_counter() - started < 2.0
    assert "private" not in redact('数据库mysql://name:private@host API_TOKEN="private" db_password=private')


def test_root_budget_hard_returns_before_slow_business_delegate_and_rejects_late_result(tmp_path, monkeypatch):
    release, completed = threading.Event(), threading.Event()

    def slow_capabilities(_adapter):
        release.wait(2)
        completed.set()
        return {"source": "mock slow ERP contract", "query": {}, "collected_at": datetime.now(timezone.utc).isoformat(),
                "business_at": None, "payload": {"version_supported": True}, "limitations": []}

    monkeypatch.setattr(ERPAdapter, "capabilities", slow_capabilities)
    settings = Settings(tmp_path, ["http://127.0.0.1:8090"])
    toolset = ERPNextSupportToolset(ERPConfig(base_url="http://127.0.0.1:8090", api_key="mock-key-12345", api_secret="mock-secret-12345"),
                                  "DN-BOUND", "2026-10-05T10:00:00+08:00", authorize=lambda: True)
    delegate = next(tool for tool in toolset.tools if tool.name == "erp_capabilities")
    budget = Budget(settings, lambda: True)
    tool = BudgetTool(name=delegate.name, description=delegate.description, parameters=delegate.parameters,
                      delegate=delegate, budget=budget, timeout=0.05)
    context = ToolInvokeContext(llm=EvidenceContextLLM(), max_token_count=8192, tool_call_id="slow-budget-contract",
                                tool_name=tool.name, tool_number=1)
    started = time.perf_counter()
    try:
        result = tool.invoke({}, context)
        assert time.perf_counter() - started < 0.3
        assert result.status == StructuredToolResultStatus.ERROR and result.error == "tool_timeout"
        assert result.data is None
    finally:
        release.set()
    assert completed.wait(1)
    assert result.status == StructuredToolResultStatus.ERROR and result.data is None
