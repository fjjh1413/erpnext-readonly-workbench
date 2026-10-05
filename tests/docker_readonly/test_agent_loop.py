"""Run the upstream Agent loop with an explicitly scripted LLM.

These are architecture integration tests, not real-model diagnosis evaluations.
Only the Docker backend and LLM responses are replaced: ToolExecutor, Tool.invoke,
parameter validation, the Agent loop, and its structured records run unchanged.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from litellm import ModelResponse

from holmes.core.llm import ContextWindowUsage, LLM
from holmes.core.docker_diagnosis import run_diagnosis
from holmes.core.models import ToolCallResult
from holmes.core.tool_calling_llm import ToolCallingLLM
from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus
from holmes.core.tools_utils.tool_executor import ToolExecutor
from holmes.plugins.toolsets.docker_readonly import (
    DockerLabBackend,
    DockerReadonlyToolset,
)
from holmes.plugins.toolsets.docker_readonly import security
from holmes.plugins.toolsets.docker_readonly.security import LabConfig, ScopeError


ALLOWED_TOOLS = {
    "docker_lab_logs",
    "docker_lab_health",
    "docker_lab_mysql",
    "docker_lab_redis",
    "docker_lab_config",
}


def scripted_response(content: str | None, calls: list[dict] | None = None) -> ModelResponse:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return ModelResponse(
        model="scripted-local-mock",
        choices=[{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if calls else "stop",
        }],
        usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    )


def scripted_call(call_id: str, name: str, params: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(params)},
    }


class ScriptedMockLLM(LLM):
    """Local responses only; completion never contacts a model provider."""

    def __init__(self, responses: list[ModelResponse]):
        self.model = "scripted-local-mock"
        self.responses = iter(responses)
        self.requests: list[dict] = []

    def get_context_window_size(self) -> int:
        return 128000

    def get_maximum_output_token(self) -> int:
        return 4096

    def count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> ContextWindowUsage:
        return ContextWindowUsage(
            total_tokens=max(1, len(json.dumps(messages, ensure_ascii=False)) // 4),
            tools_tokens=0,
            system_tokens=0,
            user_tokens=0,
            tools_to_call_tokens=0,
            assistant_tokens=0,
            other_tokens=0,
        )

    def completion(self, **kwargs: Any) -> ModelResponse:
        self.requests.append(kwargs)
        return next(self.responses)


@pytest.fixture
def readonly_executor(tmp_path, monkeypatch):
    project = "holmes-lab-012345abcdef"
    runtime_root = tmp_path / ".runtime"
    secret_dir = runtime_root / project / "secrets"
    secret_dir.mkdir(parents=True)
    monkeypatch.setattr(security, "LAB_RUNTIME_ROOT", runtime_root, raising=False)
    toolset = DockerReadonlyToolset(config={
        "project": project,
        "lab_id": "a" * 32,
        "secret_dir": str(secret_dir),
    })
    return ToolExecutor([toolset])


def test_upstream_loop_keeps_only_lab_tools_and_structured_audit(readonly_executor, monkeypatch):
    backend_calls: list[tuple[str, str]] = []

    def health(_backend, service):
        backend_calls.append(("health", service))
        return {
            "service": service,
            "state": {
                "status": "running", "running": True, "health": "unhealthy",
                "exit_code": 0, "oom_killed": False,
            },
        }

    def logs(_backend, service, tail, since_seconds, contains):
        backend_calls.append(("logs", service))
        return {
            "service": service,
            "lines": ["database connection refused"],
            "truncated": False,
            "tail": tail,
            "since_seconds": since_seconds,
        }

    monkeypatch.setattr(DockerLabBackend, "health", health)
    monkeypatch.setattr(DockerLabBackend, "logs", logs)
    assert set(readonly_executor.tools_by_name) == ALLOWED_TOOLS

    report = {
        "confirmed_facts": ["api 容器报告 unhealthy"],
        "candidate_causes": ["数据库连接可能不可用"],
        "supporting_evidence": ["call-health", "call-logs"],
        "missing_information": ["尚未执行数据库连通性检查"],
        "next_steps": ["执行只读数据库连接检查"],
    }
    response_format = {"type": "json_object"}
    llm = ScriptedMockLLM([
        scripted_response(None, [
            scripted_call("call-health", "docker_lab_health", {"service": "api"}),
            scripted_call("call-logs", "docker_lab_logs", {"service": "api"}),
        ]),
        scripted_response(json.dumps(report, ensure_ascii=False)),
    ])
    agent = ToolCallingLLM(readonly_executor, max_steps=3, llm=llm, tool_results_dir=None)
    result = agent.call(
        [{"role": "user", "content": "诊断隔离测试应用的错误。"}],
        response_format=response_format,
    )

    assert result.num_llm_calls == 2
    assert json.loads(result.result) == report
    assert sorted(backend_calls) == [("health", "api"), ("logs", "api")]
    assert all(
        {tool["function"]["name"] for tool in request["tools"]} == ALLOWED_TOOLS
        for request in llm.requests
    )
    assert all(request["response_format"] == response_format for request in llm.requests)
    assert result.tool_calls is not None
    by_id = {call.tool_call_id: call for call in result.tool_calls}
    assert set(by_id) == {"call-health", "call-logs"}
    for record in by_id.values():
        assert isinstance(record, ToolCallResult)
        assert isinstance(record.result, StructuredToolResult)
        assert record.result.status == StructuredToolResultStatus.SUCCESS
        assert record.result.elapsed_seconds is not None
        assert record.result.elapsed_seconds >= 0
        assert record.result.params["service"] == "api"
        assert record.toolset_name == readonly_executor.enabled_toolsets[0].name
    assert json.loads(by_id["call-health"].result.data)["state"]["health"] == "unhealthy"
    assert json.loads(by_id["call-logs"].result.data)["lines"] == ["database connection refused"]
    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert {message["tool_call_id"] for message in tool_messages} == set(by_id)
    assert all("tool_call_metadata=" in message["content"] for message in tool_messages)


@pytest.mark.parametrize("tool_name,params", [
    ("docker_lab_health", {"service": "production"}),
    ("docker_lab_health", {"service": "api", "container_id": "outside-lab"}),
    ("run_bash_command", {"command": "docker restart production"}),
])
def test_scripted_model_cannot_escape_tool_or_service_scope(readonly_executor, monkeypatch, tool_name, params):
    def prohibited_backend(*_args, **_kwargs):
        pytest.fail("Rejected scope must not reach the Docker backend")

    monkeypatch.setattr(DockerLabBackend, "health", prohibited_backend)
    llm = ScriptedMockLLM([
        scripted_response(None, [scripted_call("call-rejected", tool_name, params)]),
        scripted_response("证据不足：采证调用被拒绝。"),
    ])
    agent = ToolCallingLLM(readonly_executor, max_steps=3, llm=llm, tool_results_dir=None)
    result = agent.call([{"role": "user", "content": "仅诊断隔离测试应用。"}])
    assert result.tool_calls is not None and len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record.tool_call_id == "call-rejected"
    assert record.result.status == StructuredToolResultStatus.ERROR
    assert record.result.error
    assert result.num_llm_calls == 2
    assert set(readonly_executor.tools_by_name) == ALLOWED_TOOLS


def test_model_mode_keeps_real_records_rejects_fake_citations_and_redacts(readonly_executor, monkeypatch):
    config = readonly_executor.enabled_toolsets[0].config
    generated_secret = "f" * 64
    for name in security.SECRET_FILES:
        with (config.secret_dir / name).open("x", encoding="utf-8") as stream:
            stream.write(generated_secret)

    monkeypatch.setattr(DockerLabBackend, "health", lambda _backend, service: {
        "service": service,
        "state": {"status": "running", "running": True, "health": "healthy", "exit_code": 0},
    })
    monkeypatch.setattr(DockerLabBackend, "logs", lambda _backend, service, tail, since_seconds, contains: {
        "service": service, "lines": ["dependency check completed"], "truncated": False,
    })
    monkeypatch.setattr(DockerLabBackend, "config", lambda _backend: {
        "service": "api", "config": {
            "APP_MODE": "test-lab", "MYSQL_HOST": "mysql", "MYSQL_PORT": "3306",
            "REDIS_HOST": "redis", "REDIS_PORT": "6379",
        },
    })
    monkeypatch.setattr(DockerLabBackend, "mysql", lambda _backend: {
        "service": "mysql", "reachable": True, "check": "SELECT 1",
    })
    monkeypatch.setattr(DockerLabBackend, "redis", lambda _backend: {
        "service": "redis", "reachable": True, "check": "PING",
    })
    proposal = {
        "candidate_causes": [
            {"code": "needs_further_investigation", "confidence": "low",
             "statement": "日志无法解释业务故障。", "evidence_ids": ["call-model-logs"]},
            {"code": "fabricated", "confidence": "high",
             "statement": "此引用不存在。", "evidence_ids": ["made-up-id"]},
        ],
        "missing_information": ["缺少失败业务请求。"],
        "next_steps": [f"检查日志中的 password={generated_secret}。"],
    }
    llm = ScriptedMockLLM([
        scripted_response(None, [scripted_call("call-model-logs", "docker_lab_logs", {"service": "api"})]),
        scripted_response(json.dumps(proposal, ensure_ascii=False)),
    ])

    report = run_diagnosis(config, llm=llm)

    assert report.mode == "evidence-with-model"
    assert report.metrics.tool_calls == 10
    assert report.metrics.successful_calls == 10
    assert report.metrics.tool_success_rate == 1
    assert "call-model-logs" in {call.tool_call_id for call in report.tool_calls}
    assert "call-model-logs" in {evidence.id for evidence in report.supporting_evidence}
    assert [cause.code for cause in report.model_hypotheses] == ["needs_further_investigation"]
    assert not report.candidate_causes
    assert any("不存在" in text for text in report.missing_information)
    assert generated_secret not in report.model_dump_json()
    assert "[REDACTED]" in "".join(report.next_steps)
    assert len(llm.requests) == 2


def test_final_privacy_barrier_respects_rejected_credential_files(readonly_executor, monkeypatch):
    """A rejected credential path must not get a second, unchecked file read."""
    config = readonly_executor.enabled_toolsets[0].config
    guard_checks: list[str] = []

    def reject_credential(_config, name):
        guard_checks.append(name)
        raise ScopeError("Credential path is linked outside the generated lab")

    def forbid_unchecked_read(path, *_args, **_kwargs):
        pytest.fail(f"Unchecked read bypassed the credential path guard: {path.name}")

    monkeypatch.setattr(LabConfig, "secret", reject_credential)
    monkeypatch.setattr(Path, "read_text", forbid_unchecked_read)
    monkeypatch.setattr(DockerLabBackend, "health", lambda _backend, service: {
        "service": service, "state": {"status": "running", "running": True, "health": "healthy"},
    })
    monkeypatch.setattr(DockerLabBackend, "logs", lambda _backend, service, tail, since_seconds, contains: {
        "service": service, "lines": [], "truncated": False,
    })
    monkeypatch.setattr(DockerLabBackend, "config", lambda _backend: {
        "service": "api", "config": {
            "APP_MODE": "test-lab", "MYSQL_HOST": "mysql", "MYSQL_PORT": "3306",
            "REDIS_HOST": "redis", "REDIS_PORT": "6379",
        },
    })
    monkeypatch.setattr(DockerLabBackend, "mysql", lambda _backend: {
        "service": "mysql", "reachable": False, "check": "SELECT 1",
    })
    monkeypatch.setattr(DockerLabBackend, "redis", lambda _backend: {
        "service": "redis", "reachable": False, "check": "PING",
    })

    report = run_diagnosis(config)

    assert guard_checks
    assert report.metrics.tool_calls == 9
    assert report.missing_information


def test_completed_model_tool_records_survive_later_provider_failure(readonly_executor, monkeypatch):
    """The scripted provider fails after one completed upstream tool invocation."""
    config = readonly_executor.enabled_toolsets[0].config
    monkeypatch.setattr(DockerLabBackend, "health", lambda _backend, service: {
        "service": service,
        "state": {"status": "running", "running": True, "health": "healthy", "exit_code": 0},
    })
    monkeypatch.setattr(DockerLabBackend, "logs", lambda _backend, service, tail, since_seconds, contains: {
        "service": service, "lines": ["completed read before provider failure"], "truncated": False,
    })
    monkeypatch.setattr(DockerLabBackend, "config", lambda _backend: {
        "service": "api", "config": {
            "APP_MODE": "test-lab", "MYSQL_HOST": "mysql", "MYSQL_PORT": "3306",
            "REDIS_HOST": "redis", "REDIS_PORT": "6379",
        },
    })
    monkeypatch.setattr(DockerLabBackend, "mysql", lambda _backend: {
        "service": "mysql", "reachable": True, "check": "SELECT 1",
    })
    monkeypatch.setattr(DockerLabBackend, "redis", lambda _backend: {
        "service": "redis", "reachable": True, "check": "PING",
    })
    # Only the first response exists. completion() raises on the second request;
    # no real provider or Docker daemon is contacted at any point.
    llm = ScriptedMockLLM([
        scripted_response(None, [scripted_call(
            "call-before-model-failure", "docker_lab_logs", {"service": "api"},
        )]),
    ])

    report = run_diagnosis(config, llm=llm)

    assert len(llm.requests) == 2
    assert report.mode == "evidence-with-model"
    assert report.metrics.tool_calls == 10
    assert report.metrics.successful_calls == 10
    assert report.metrics.tool_success_rate == 1
    records = {call.tool_call_id: call for call in report.tool_calls}
    assert records["call-before-model-failure"].result.status == StructuredToolResultStatus.SUCCESS
    assert "call-before-model-failure" in {evidence.id for evidence in report.supporting_evidence}
    assert any("模型分析未完成" in text and "保留全部已完成采证记录" in text
               for text in report.missing_information)
    assert not report.model_hypotheses
