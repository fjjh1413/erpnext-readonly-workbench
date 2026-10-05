"""Tests for fixed internal probes; all drivers and HTTP transports are mocked."""

import importlib.util
import io
import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location("readonly_probe", Path(__file__).parents[1] / "app" / "probe.py")
assert SPEC and SPEC.loader
probe_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe_module)


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {name: secrets.token_hex(32) for name in ("mysql_probe_password", "redis_probe_password")}
    for name, value in values.items():
        (tmp_path / name).write_text(value, encoding="utf-8")
    monkeypatch.setattr(probe_module, "SECRET_DIR", tmp_path)
    return values


class MySQLConnection:
    def __init__(self, queries: list[str], result: tuple[int, ...] = (1,)):
        self.queries = queries
        self.result = result

    def __enter__(self) -> "MySQLConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def cursor(self) -> "MySQLConnection":
        return self

    def execute(self, query: str) -> None:
        self.queries.append(query)

    def fetchone(self) -> tuple[int, ...]:
        return self.result


class RedisConnection:
    def __init__(self, operations: list[str], pong: bool = True):
        self.operations = operations
        self.pong = pong

    def __enter__(self) -> "RedisConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def ping(self) -> bool:
        self.operations.append("PING")
        return self.pong


class HTTPResponse(io.BytesIO):
    def __init__(self, payload: bytes, code: int = 200):
        super().__init__(payload)
        self.code = code
        self.read_limits: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_limits.append(size)
        return super().read(size)


def install_http(monkeypatch: pytest.MonkeyPatch, response: HTTPResponse | BaseException) -> dict[str, Any]:
    calls: dict[str, Any] = {"handlers": [], "requests": []}
    class Opener:
        def open(self, url: str, timeout: int) -> HTTPResponse:
            calls["requests"].append((url, timeout))
            if isinstance(response, BaseException):
                raise response
            return response
    def build_opener(*handlers: Any) -> Opener:
        calls["handlers"] = handlers
        return Opener()
    monkeypatch.setattr(probe_module.urllib.request, "build_opener", build_opener)
    return calls


def test_mysql_uses_fixed_endpoint_probe_identity_and_select_one(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[dict[str, Any]] = []
    queries: list[str] = []
    def connect(**arguments: Any) -> MySQLConnection:
        connections.append(arguments)
        return MySQLConnection(queries)
    monkeypatch.setattr(probe_module.pymysql, "connect", connect)
    result = probe_module.mysql_check()
    assert queries == ["SELECT 1"]
    assert connections == [{
        "host": "mysql", "port": 3306, "user": "probe",
        "password": credentials["mysql_probe_password"],
        "connect_timeout": 2, "read_timeout": 2, "write_timeout": 2, "autocommit": True,
    }]
    assert result["reachable"] is True
    assert result["service"] == "mysql" and result["check"] == "SELECT 1"
    assert set(result) == {"service", "reachable", "check", "elapsed_ms"}
    assert result["elapsed_ms"] >= 0


def test_redis_uses_fixed_endpoint_auth_probe_and_ping_only(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[dict[str, Any]] = []
    operations: list[str] = []
    def connect(**arguments: Any) -> RedisConnection:
        connections.append(arguments)
        return RedisConnection(operations)
    monkeypatch.setattr(probe_module.redis, "Redis", connect)
    result = probe_module.redis_check()
    assert connections == [{
        "host": "redis", "port": 6379, "username": "probe",
        "password": credentials["redis_probe_password"],
        "socket_connect_timeout": 2, "socket_timeout": 2, "protocol": 2,
    }]
    assert operations == ["PING"]
    assert result["reachable"] is True
    assert result["service"] == "redis" and result["check"] == "PING"
    assert set(result) == {"service", "reachable", "check", "elapsed_ms"}


@pytest.mark.parametrize("service,error_kind,error_code,expected", [
    ("mysql", "authentication", 1045, "authentication_failed"),
    ("mysql", "connection", 2003, "connection_failed"),
    ("mysql", "timeout", None, "timeout"),
    ("redis", "authentication", None, "authentication_failed"),
    ("redis", "connection", None, "connection_failed"),
    ("redis", "timeout", None, "timeout"),
])
def test_failure_output_contains_only_safe_fields(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    service: str, error_kind: str, error_code: int | None, expected: str
) -> None:
    message = f"dsn://user:{credentials[f'{service}_probe_password']}@production.invalid"
    if error_kind == "timeout":
        exception: BaseException = TimeoutError(message)
    elif service == "mysql":
        exception = probe_module.pymysql.OperationalError(error_code, message)
    elif error_kind == "authentication":
        exception = probe_module.redis.AuthenticationError(message)
    else:
        exception = probe_module.redis.ConnectionError(message)
    def fail(**arguments: Any) -> None:
        raise exception
    monkeypatch.setattr(probe_module.pymysql if service == "mysql" else probe_module.redis,
                        "connect" if service == "mysql" else "Redis", fail)
    result = getattr(probe_module, f"{service}_check")()
    assert result["reachable"] is False and result["error"] == expected
    assert set(result).issubset({"service", "reachable", "check", "error", "error_code", "elapsed_ms"})
    if error_code is not None:
        assert result["error_code"] == error_code
    serialized = json.dumps(result)
    assert "production.invalid" not in serialized
    assert not any(value in serialized for value in credentials.values())


@pytest.mark.parametrize("service", ["mysql", "redis"])
def test_invalid_runtime_secret_is_rejected_before_a_connection(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    (probe_module.SECRET_DIR / f"{service}_probe_password").write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(probe_module.pymysql if service == "mysql" else probe_module.redis,
                        "connect" if service == "mysql" else "Redis", lambda **kwargs: pytest.fail("No connection allowed"))
    result = getattr(probe_module, f"{service}_check")()
    assert result["reachable"] is False and result["error"] == "unexpected_response"


@pytest.mark.parametrize("service", ["mysql", "redis"])
def test_unexpected_fixed_operation_response_is_not_success(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    if service == "mysql":
        monkeypatch.setattr(probe_module.pymysql, "connect", lambda **kwargs: MySQLConnection([], result=(2,)))
    else:
        monkeypatch.setattr(probe_module.redis, "Redis", lambda **kwargs: RedisConnection([], pong=False))
    result = getattr(probe_module, f"{service}_check")()
    assert result["reachable"] is False and result["error"] == "unexpected_response"


@pytest.mark.parametrize("status_code,status,dependencies", [
    (200, "ready", {"mysql": "ok", "redis": "ok"}),
    (503, "not_ready", {"mysql": "unavailable", "redis": "ok"}),
])
def test_ready_reads_fixed_endpoint_with_no_proxy_no_redirect_and_bounded_body(
    monkeypatch: pytest.MonkeyPatch, status_code: int, status: str, dependencies: dict[str, str]
) -> None:
    body = {"status": status, "dependencies": dependencies}
    response = HTTPResponse(json.dumps(body).encode(), code=status_code)
    calls = install_http(monkeypatch, response)
    assert probe_module.ready_check() == {"status_code": status_code, "body": body}
    assert calls["requests"] == [("http://api:8000/ready", 5)]
    proxy = next(handler for handler in calls["handlers"] if isinstance(handler, urllib.request.ProxyHandler))
    assert proxy.proxies == {}
    assert any(isinstance(handler, probe_module.NoRedirect) for handler in calls["handlers"])
    assert response.read_limits == [4097]


def test_ready_redirect_handler_never_creates_an_external_request() -> None:
    handler = probe_module.NoRedirect()
    assert handler.redirect_request(
        urllib.request.Request("http://api:8000/ready"), None, 302, "redirect", {},
        "https://production.invalid/ready",
    ) is None


def test_ready_handles_http_error_locally_without_following_location(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError("http://api:8000/ready", 302, "redirect",
                                   {"Location": "https://production.invalid/ready"}, io.BytesIO(b"redirect"))
    calls = install_http(monkeypatch, error)
    result = probe_module.ready_check()
    assert calls["requests"] == [("http://api:8000/ready", 5)]
    assert result["status_code"] == 0 and result["body"] == {}
    assert "production.invalid" not in json.dumps(result)


def test_ready_body_limit_is_enforced_before_json_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    response = HTTPResponse(b" " * 4097)
    install_http(monkeypatch, response)
    result = probe_module.ready_check()
    assert response.read_limits == [4097]
    assert result == {"status_code": 0, "body": {}, "error_type": "ValueError"}


@pytest.mark.parametrize("body", [
    [], None, "invalid", {"status": "ready"},
    {"status": "unknown", "dependencies": {"mysql": "ok", "redis": "ok"}},
    {"status": "ready", "dependencies": []},
    {"status": "ready", "dependencies": {"mysql": "ok", "redis": "unknown"}},
    {"status": "ready", "dependencies": {"mysql": "ok", "redis": "ok", "production": "ok"}},
])
def test_ready_rejects_invalid_schema(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    install_http(monkeypatch, HTTPResponse(json.dumps(body).encode()))
    result = probe_module.ready_check()
    assert result["status_code"] == 0 and result["body"] == {}


def test_ready_drops_extra_fields_that_may_contain_secrets(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"status": "ready", "dependencies": {"mysql": "ok", "redis": "ok"},
            "password": credentials["mysql_probe_password"], "headers": {"Authorization": "private"}}
    install_http(monkeypatch, HTTPResponse(json.dumps(body).encode()))
    result = probe_module.ready_check()
    assert set(result["body"]) == {"status", "dependencies"}
    assert not any(value in json.dumps(result) for value in credentials.values())


def test_ready_network_errors_never_return_raw_driver_or_url_text(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exception = urllib.error.URLError(f"production.invalid/{credentials['redis_probe_password']}")
    install_http(monkeypatch, exception)
    assert probe_module.ready_check() == {"status_code": 0, "body": {}, "error_type": "URLError"}


@pytest.mark.parametrize("action", ["mysql", "redis", "ready"])
def test_total_deadline_covers_fixed_probe_and_is_cancelled(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    handlers: list[Any] = []
    alarms: list[int] = []
    monkeypatch.setattr(probe_module.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(probe_module.signal, "signal", lambda signum, handler: handlers.append((signum, handler)))
    monkeypatch.setattr(probe_module.signal, "alarm", lambda timeout: alarms.append(timeout), raising=False)
    def expire(**arguments: Any) -> None:
        handlers[0][1](14, None)
    if action == "mysql":
        monkeypatch.setattr(probe_module.pymysql, "connect", expire)
    elif action == "redis":
        monkeypatch.setattr(probe_module.redis, "Redis", expire)
    else:
        class Opener:
            def open(self, url: str, timeout: int) -> None:
                handlers[0][1](14, None)
        monkeypatch.setattr(probe_module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(sys, "argv", ["probe.py", action])
    probe_module.main()
    result = json.loads(capsys.readouterr().out)
    assert alarms == [3, 0]
    assert handlers[0][0] == 14
    if action == "ready":
        assert result["status_code"] == 0 and result["body"] == {}
    else:
        assert result["reachable"] is False and result["error"] == "timeout"
    assert not any(value in json.dumps(result) for value in credentials.values())


@pytest.mark.parametrize("arguments", [["mysql;DROP TABLE x"], ["mysql", "--host", "production.invalid"], ["sh"]])
def test_cli_rejects_commands_hosts_and_sql_arguments(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["probe.py", *arguments])
    monkeypatch.setattr(probe_module.pymysql, "connect", lambda **kwargs: pytest.fail("No connection allowed"))
    monkeypatch.setattr(probe_module.redis, "Redis", lambda **kwargs: pytest.fail("No connection allowed"))
    with pytest.raises(SystemExit) as caught:
        probe_module.main()
    assert caught.value.code == 2
