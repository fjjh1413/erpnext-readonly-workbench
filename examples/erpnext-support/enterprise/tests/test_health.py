"""The container probe never imports Agent, DB drivers or arbitrary targets."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SPEC = importlib.util.spec_from_file_location("enterprise_health_test", Path(__file__).resolve().parents[1] / "health.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


@pytest.mark.parametrize("service,endpoint", [("api", "http://127.0.0.1:8765/ready"), ("worker", "http://api:8765/ready")])
def test_fixed_target_proxy_and_deadline(monkeypatch, service, endpoint):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = json.dumps({key: True for key in ("ready", "database_ready", "broker_ready", "worker_ready")}).encode()
    opener = MagicMock()
    opener.open.return_value = response
    handlers = []
    monkeypatch.setattr(health.urllib.request, "build_opener", lambda *args: (handlers.extend(args), opener)[1])
    assert health.check_ready(service) is True
    request = opener.open.call_args.args[0]
    assert request.full_url == endpoint and request.get_header("Host") == "localhost"
    assert opener.open.call_args.kwargs == {"timeout": 3}
    assert response.read.call_args.args == (4097,)
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, None, {}, "https://foreign.invalid") is None


@pytest.mark.parametrize("body", [b"x" * 4097, b"[]", b'{"ready":true}',
    json.dumps({"ready": True, "database_ready": True, "broker_ready": True, "worker_ready": False}).encode(),
    json.dumps({"ready": True, "database_ready": True, "broker_ready": True, "worker_ready": 1}).encode()],
    ids=["too_large", "list", "missing_fields", "missing_heartbeat", "numeric_boolean"])
def test_invalid_or_incomplete_ready_is_not_healthy(monkeypatch, body):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = body
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(health.urllib.request, "build_opener", lambda *args: opener)
    assert health.check_ready("worker") is False


def test_unknown_target_is_rejected():
    with pytest.raises(ValueError):
        health.check_ready("foreign")
