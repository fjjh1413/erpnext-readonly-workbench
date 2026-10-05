"""Mocked safety/protocol tests; none inspect or connect to existing Docker targets."""

import io
import json
import logging
import socket
import subprocess
from copy import deepcopy

import pytest
from pydantic import ValidationError

from holmes.core.tools import StructuredToolResultStatus, ToolInvokeContext
from holmes.plugins.toolsets.docker_readonly import DockerLabBackend, DockerReadonlyToolset, LabConfig, redact
from holmes.plugins.toolsets.docker_readonly import backend, security
from holmes.plugins.toolsets.docker_readonly.backend import BoundedDockerRunner, CommandOutput, OUTPUT_LIMIT
from holmes.plugins.toolsets.docker_readonly.security import BackendError, LAB_LABEL, PROJECT_LABEL, SERVICE_LABEL, ScopeError

PROJECT = "holmes-lab-012345abcdef"
LAB_ID = "a" * 32
MYSQL_SECRET = "generated-mysql-secret-unique"
REDIS_SECRET = "generated-redis-secret-unique"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    runtime = tmp_path / "example" / ".runtime"
    monkeypatch.setattr(security, "LAB_RUNTIME_ROOT", runtime)
    secret_dir = runtime / PROJECT / "secrets"
    secret_dir.mkdir(parents=True)
    (secret_dir / "mysql_probe_password").write_text(MYSQL_SECRET, encoding="utf-8")
    (secret_dir / "redis_probe_password").write_text(REDIS_SECRET, encoding="utf-8")
    return LabConfig(project=PROJECT, lab_id=LAB_ID, secret_dir=secret_dir)


class FakeDocker:
    def __init__(self, lab, service="api"):
        self.calls = []
        self.host = "npipe:////./pipe/docker_engine"
        self.container_id = "1" * 64
        self.network_id = "2" * 64
        self.container = {
            "Id": self.container_id,
            "Config": {
                "Labels": {PROJECT_LABEL: lab.project, LAB_LABEL: lab.lab_id, SERVICE_LABEL: service},
                "Env": ["MYSQL_HOST=mysql", "MYSQL_PORT=3306", "REDIS_HOST=redis", "REDIS_PORT=6379", "APP_MODE=test-lab", f"MYSQL_PASSWORD={MYSQL_SECRET}", "OTHER=private"],
            },
            "HostConfig": {"NetworkMode": f"{PROJECT}_isolated"},
            "NetworkSettings": {
                "Networks": {f"{PROJECT}_isolated": {"NetworkID": self.network_id}},
                "Ports": {backend.SERVICE_PORTS[service]: [{"HostIp": "127.0.0.1", "HostPort": "32123"}]},
            },
            "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}, "ExitCode": 0, "OOMKilled": False},
        }
        self.network = {
            "Id": self.network_id, "Internal": True,
            "Labels": {PROJECT_LABEL: lab.project, LAB_LABEL: lab.lab_id},
            "Containers": {self.container_id: {}},
        }
        self.peers = None
        self.probe = None
        self.probe_output = None
        self.ids = self.container_id
        self.log_output = CommandOutput("line-one\nline-two\n")

    def run(self, args, *, allow_truncation=False):
        self.calls.append((args, allow_truncation))
        if args == ["context", "inspect"]:
            return CommandOutput(json.dumps([{"Name": "desktop-linux", "Endpoints": {"docker": {"Host": self.host}}}]))
        assert args[:2] == ["--context", "desktop-linux"]
        command = args[2:]
        if command[0] == "ps":
            if command[-1].endswith("=probe") and self.probe is not None:
                return CommandOutput(self.probe["Id"])
            return CommandOutput(self.ids)
        if command[:2] == ["network", "inspect"]:
            return CommandOutput(json.dumps([self.network]))
        if command[0] == "inspect":
            if self.probe is not None:
                lookup = {self.container["Id"]: self.container, self.probe["Id"]: self.probe}
                return CommandOutput(json.dumps([lookup[value] for value in command[3:]]))
            return CommandOutput(json.dumps(self.peers if self.peers is not None and len(command) > 4 else [self.container]))
        if command[0] == "logs":
            return self.log_output
        if command[0] == "exec" and self.probe is not None:
            service = command[-1]
            result = self.probe_output or {"service": service, "reachable": True, "check": "SELECT 1" if service == "mysql" else "PING"}
            return CommandOutput(json.dumps(result))
        raise AssertionError("A non-read-only Docker command was attempted")

    def add_probe(self, lab):
        self.probe = deepcopy(self.container)
        self.probe["Id"] = "3" * 64
        self.probe["Config"]["Labels"][SERVICE_LABEL] = "probe"
        self.probe["Config"]["Image"] = f"{lab.project}-api:lab"
        self.probe["HostConfig"]["ReadonlyRootfs"] = True
        self.probe["Mounts"] = [
            {"Type": "bind", "Source": str(lab.secret_dir / name), "Destination": f"/run/secrets/{name}", "RW": False}
            for name in ("mysql_probe_password", "redis_probe_password")
        ]
        self.probe["NetworkSettings"]["Ports"] = {}
        self.network["Containers"][self.probe["Id"]] = {}


def make_backend(lab, service="api"):
    runner = FakeDocker(lab, service)
    return DockerLabBackend(lab, runner), runner


def make_tool(lab, name="docker_lab_logs"):
    toolset = DockerReadonlyToolset(lab.model_dump())
    return toolset, next(tool for tool in toolset.tools if tool.name == name)


def context():
    return ToolInvokeContext.model_construct(llm=None, max_token_count=10000, tool_call_id="test", tool_name="lab")


@pytest.mark.parametrize("params", [
    {"service": "production"}, {"service": "api", "container": "prod"},
    {"service": "api", "tail": 201}, {"service": "api", "tail": 0},
    {"service": "api", "tail": "2"}, {"service": "api", "tail": True},
    {"service": "api", "since_seconds": 3601}, {"service": "api", "since_seconds": 0},
    {"service": "api", "contains": "x" * 101}, {"service": "api", "contains": "\n"},
    {"service": "api", "host": "production"}, {"service": "api", "sql": "DELETE FROM records"},
])
def test_strict_invalid_parameters_never_reach_backend(lab, params, monkeypatch):
    toolset, tool = make_tool(lab)
    monkeypatch.setattr(toolset.backend, "logs", lambda **kwargs: pytest.fail("Backend must not execute"))
    result = tool.invoke(params, context())
    assert result.status == StructuredToolResultStatus.ERROR
    assert result.params is None


def test_raw_invalid_secret_does_not_appear_in_logs_or_results(lab, caplog):
    _, tool = make_tool(lab)
    with caplog.at_level(logging.INFO):
        result = tool.invoke({"service": MYSQL_SECRET, "password": REDIS_SECRET}, context())
    text = result.model_dump_json() + caplog.text
    assert MYSQL_SECRET not in text and REDIS_SECRET not in text


@pytest.mark.parametrize("operation,params", [
    ("mysql", {"host": "production"}), ("redis", {"port": 6379}),
    ("config", {"env": "MYSQL_PASSWORD"}), ("mysql", {"sql": "UPDATE users SET admin=1"}),
])
def test_probe_and_config_do_not_accept_destination_or_query_parameters(lab, operation, params, monkeypatch):
    toolset, tool = make_tool(lab, f"docker_lab_{operation}")
    monkeypatch.setattr(toolset.backend, operation, lambda **kwargs: pytest.fail("Backend must not execute"))
    result = tool.invoke(params, context())
    assert result.status == StructuredToolResultStatus.ERROR
    assert result.params is None


def test_literal_filter_is_not_echoed_into_call_trace(lab):
    toolset, tool = make_tool(lab)
    runner = FakeDocker(lab)
    runner.log_output = CommandOutput(f"contains {MYSQL_SECRET}\n")
    toolset.backend.runner = runner
    result = tool.invoke({"service": "api", "contains": MYSQL_SECRET}, context())
    assert result.status == StructuredToolResultStatus.SUCCESS
    assert MYSQL_SECRET not in result.model_dump_json()
    assert result.params["filter_applied"] is True


@pytest.mark.parametrize("host", ["tcp://127.0.0.1:2375", "tcp://production:2376", "ssh://root@production", "npipe:////remote/pipe/docker_engine"])
def test_remote_contexts_are_rejected_before_container_reads(lab, host):
    target, runner = make_backend(lab)
    runner.host = host
    with pytest.raises(ScopeError):
        target.health("api")
    assert len(runner.calls) == 1


def test_docker_host_override_is_rejected_without_cli_access(lab, monkeypatch):
    target, runner = make_backend(lab)
    monkeypatch.setenv("DOCKER_HOST", "tcp://production:2375")
    with pytest.raises(ScopeError):
        target.health("api")
    assert not runner.calls


@pytest.mark.parametrize("change", [
    lambda docker: docker.container["Config"]["Labels"].update({LAB_LABEL: "b" * 32}),
    lambda docker: docker.container["Config"]["Labels"].update({PROJECT_LABEL: "production"}),
    lambda docker: docker.container["HostConfig"].update({"NetworkMode": "host"}),
    lambda docker: docker.container["HostConfig"].update({"NetworkMode": "container:production"}),
    lambda docker: docker.container["NetworkSettings"]["Networks"].update({"shared": {"NetworkID": "3" * 64}}),
    lambda docker: docker.network.update({"Internal": False}),
    lambda docker: docker.network["Labels"].update({LAB_LABEL: "b" * 32}),
    lambda docker: docker.container["NetworkSettings"]["Ports"]["8000/tcp"][0].update({"HostIp": "0.0.0.0"}),
    lambda docker: setattr(docker, "ids", docker.container_id + "\n" + "3" * 64),
])
def test_container_and_network_scope_fail_closed(lab, change):
    target, runner = make_backend(lab)
    change(runner)
    with pytest.raises(ScopeError):
        target.health("api")


def test_shared_network_with_foreign_peer_is_rejected(lab):
    target, runner = make_backend(lab)
    foreign = deepcopy(runner.container)
    foreign["Id"] = "3" * 64
    foreign["Config"]["Labels"][PROJECT_LABEL] = "production"
    runner.network["Containers"][foreign["Id"]] = {}
    runner.peers = [runner.container, foreign]
    with pytest.raises(ScopeError):
        target.health("api")


def test_health_returns_observed_unhealthy_as_success(lab):
    toolset, tool = make_tool(lab, "docker_lab_health")
    runner = FakeDocker(lab)
    runner.container["State"]["Health"]["Status"] = "unhealthy"
    toolset.backend.runner = runner
    result = tool.invoke({"service": "api"}, context())
    assert result.status == StructuredToolResultStatus.SUCCESS
    assert result.data["state"]["health"] == "unhealthy"


def test_logs_are_bounded_filtered_redacted_and_read_only(lab):
    target, runner = make_backend(lab)
    runner.log_output = CommandOutput(f"ignore\nerror password={MYSQL_SECRET}\nerror token=untracked\n", truncated=True)
    result = target.logs("api", tail=1, since_seconds=300, contains="error")
    assert result["lines"] == ["error token=[REDACTED]"]
    assert result["truncated"] is True
    command = runner.calls[-1][0]
    assert "300s" in command and "--tail" in command
    assert not {"exec", "start", "stop", "restart", "rm", "run"}.intersection(command)


def test_config_returns_only_non_sensitive_whitelist(lab):
    target, _ = make_backend(lab)
    data = target.config()
    assert set(data["config"]) == set(backend.CONFIG_KEYS)
    assert MYSQL_SECRET not in json.dumps(data)


def test_recursive_redaction_known_and_generic_secrets():
    value = {
        "password": "hide-me", "logs": [
            "mysql://probe:sensitive@mysql/db", "Authorization: Bearer abc.def",
            'secret="two words" token=qwerty', "known raw sensitive-value",
            'json={"Authorization":"Bearer private-json"}', "Authorization: opaque-auth",
        ],
    }
    result = json.dumps(redact(value, ["sensitive-value"]))
    for secret in ("hide-me", "sensitive@", "abc.def", "two words", "qwerty", "sensitive-value", "private-json", "opaque-auth"):
        assert secret not in result


def test_labelled_peer_with_external_network_is_rejected(lab):
    target, runner = make_backend(lab)
    foreign = deepcopy(runner.container)
    foreign["Id"] = "3" * 64
    foreign["Config"]["Labels"][SERVICE_LABEL] = "redis"
    foreign["NetworkSettings"]["Networks"]["external"] = {"NetworkID": "4" * 64}
    runner.network["Containers"][foreign["Id"]] = {}
    runner.peers = [runner.container, foreign]
    with pytest.raises(ScopeError):
        target.health("api")


def test_stopped_container_static_port_bindings_are_checked(lab):
    target, runner = make_backend(lab)
    runner.container["State"]["Running"] = False
    runner.container["NetworkSettings"]["Ports"] = {}
    runner.container["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32123"}]}
    with pytest.raises(ScopeError):
        target.health("api")


def test_secret_read_is_bounded(lab):
    (lab.secret_dir / "mysql_probe_password").write_bytes(b"x" * 257)
    with pytest.raises(ScopeError, match="byte limit"):
        lab.secret("mysql_probe_password")


def test_config_loader_rejects_external_secret_directory(lab, tmp_path):
    values = lab.model_dump(mode="json")
    values["secret_dir"] = str(tmp_path / "production")
    with pytest.raises((ScopeError, ValidationError)):
        LabConfig.model_validate(values)


def test_config_loader_accepts_only_generated_project_file(lab):
    path = lab.secret_dir.parent / "config.json"
    path.write_text(lab.model_dump_json(), encoding="utf-8")
    assert LabConfig.load(path) == lab
    wrong = path.with_name("external.json")
    wrong.write_text(lab.model_dump_json(), encoding="utf-8")
    with pytest.raises(ScopeError):
        LabConfig.load(wrong)


def test_secret_link_checks_do_not_read_link_target(lab, monkeypatch):
    original = security.Path.is_junction
    monkeypatch.setattr(security.Path, "is_junction", lambda path: path.name == "secrets" or original(path))
    with pytest.raises(ScopeError):
        lab.secret("mysql_probe_password")


@pytest.mark.parametrize("service", ["mysql", "redis"])
def test_stopped_service_is_observation_without_exec_or_host_connection(lab, service, monkeypatch):
    target, runner = make_backend(lab, service)
    runner.container["State"]["Running"] = False
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("No connection permitted"))
    assert getattr(target, service)()["error"] == "container_not_running"
    assert not any("exec" in command for command, _ in runner.calls)


@pytest.mark.parametrize("service", ["mysql", "redis"])
def test_probe_exec_is_fixed_nonroot_and_never_connects_from_host(lab, service, monkeypatch):
    target, runner = make_backend(lab, service)
    runner.add_probe(lab)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("No host connection permitted"))
    assert getattr(target, service)()["reachable"] is True
    command = runner.calls[-1][0]
    assert command == ["--context", "desktop-linux", "exec", "--user", "10001:10001", runner.probe["Id"], "python", "/app/probe.py", service]
    assert MYSQL_SECRET not in " ".join(command)
    assert REDIS_SECRET not in " ".join(command)


@pytest.mark.parametrize("change", [
    lambda probe: probe["Config"].update({"Image": "production-image"}),
    lambda probe: probe["HostConfig"].update({"ReadonlyRootfs": False}),
    lambda probe: probe["HostConfig"].update({"Privileged": True}),
    lambda probe: probe["Mounts"].append({"Destination": "/var/run/docker.sock", "Type": "bind"}),
    lambda probe: probe["Mounts"][0].update({"RW": True}),
    lambda probe: probe["Mounts"][0].update({"Source": "/production/mysql_probe_password"}),
    lambda probe: probe["Mounts"][0].update({"Destination": "/run/secrets/mysql_root_password"}),
])
def test_probe_safety_checks_block_exec(lab, change):
    target, runner = make_backend(lab, "mysql")
    runner.add_probe(lab)
    change(runner.probe)
    with pytest.raises(ScopeError):
        target.mysql()
    assert not any("exec" in command for command, _ in runner.calls)


@pytest.mark.parametrize("change", [
    {"service": "production"}, {"check": "DROP TABLE users"}, {"reachable": "true"},
    {"arbitrary": "extra"}, {"elapsed_ms": 1000000}, {"error_code": "1045"},
])
def test_invalid_probe_output_is_rejected(lab, change):
    target, runner = make_backend(lab, "mysql")
    runner.add_probe(lab)
    runner.probe_output = {"service": "mysql", "check": "SELECT 1", "reachable": True, **change}
    with pytest.raises(BackendError, match="schema"):
        target.mysql()


def test_probe_observed_failure_and_output_are_sanitized(lab):
    toolset, tool = make_tool(lab, "docker_lab_mysql")
    runner = FakeDocker(lab, "mysql")
    runner.add_probe(lab)
    runner.probe_output = {"service": "mysql", "check": "SELECT 1", "reachable": False, "error": f"password={MYSQL_SECRET}", "error_code": 1045}
    toolset.backend.runner = runner
    result = tool.invoke({}, context())
    assert result.status == StructuredToolResultStatus.SUCCESS
    assert result.data["reachable"] is False
    assert MYSQL_SECRET not in result.model_dump_json()


def test_probe_container_is_never_user_selectable(lab):
    _, tool = make_tool(lab, "docker_lab_health")
    result = tool.invoke({"service": "probe"}, context())
    assert result.status == StructuredToolResultStatus.ERROR


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", timeout=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = 0
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("docker", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_docker_runner_caps_combined_output_and_uses_no_shell(monkeypatch):
    process = FakeProcess(b"x" * (OUTPUT_LIMIT + 4096), b"y" * 4096)

    def launch(args, **kwargs):
        assert args == ["docker", "logs", "test"]
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    result = BoundedDockerRunner().run(["logs", "test"], allow_truncation=True)
    assert result.truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= OUTPUT_LIMIT
    assert process.killed


def test_docker_runner_metadata_oversize_and_timeout_fail_closed(monkeypatch):
    process = FakeProcess(b"x" * (OUTPUT_LIMIT + 1))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(BackendError, match="output limit"):
        BoundedDockerRunner().run(["inspect", "test"])
    timeout_process = FakeProcess(timeout=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: timeout_process)
    with pytest.raises(BackendError, match="timeout"):
        BoundedDockerRunner().run(["inspect", "test"])
    assert timeout_process.killed
