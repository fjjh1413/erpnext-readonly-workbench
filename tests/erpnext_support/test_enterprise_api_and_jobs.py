"""Enterprise API/worker contracts. ERP diagnosis and broker are labelled mocks."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from holmes.erpnext_support import jobs
from holmes.erpnext_support.security import password_hash
from holmes.erpnext_support.server import create_app
from holmes.erpnext_support.settings import Settings
from holmes.erpnext_support.storage import Store


@pytest.fixture
def enterprise(tmp_path):
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials/lab.json").write_text(json.dumps({"api_key": "mock-only-api-key", "api_secret": "mock-only-api-secret"}), encoding="utf-8")
    settings = Settings(tmp_path, ["http://127.0.0.1:8090"], metrics_token="mock-metrics-token-" + "x" * 32)
    app = create_app(settings, diagnostic=lambda *args: {"mode": "test_mock_only", "missing": [], "metrics": {"tool_success_rate": 1}, "causes": []})
    store = app.state.store
    admin = store.create_user_unique({"username": "admin", "role": "admin", "environment_ids": [], "password_hash": password_hash("mock-admin-password")})
    with TestClient(app) as client:
        result = client.post("/api/session", json={"username": "admin", "password": "mock-admin-password"})
        assert result.status_code == 200
        client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        yield client, store, settings, admin
    store.close()


def create_user(client, username="reviewer"):
    result = client.post("/api/users", json={"username": username, "password": "mock-reviewer-password", "role": "reviewer", "environment_ids": []})
    assert result.status_code == 200
    return result.json()


def seed_job(store, admin):
    environment = store.add("environment", {"name": "Mock only", "base_url": "http://127.0.0.1:8090", "credential_ref": "lab", "erpnext_version": "15.85.1", "frappe_version": "15.87.0"})
    ticket = store.add("ticket", {"status": "pending", "environment_id": environment["id"], "delivery_note": "MOCK-DN", "description": "mock only"}, environment_id=environment["id"])
    task = store.create_task(ticket["id"], {"kind": "diagnosis", "steps": [], "request_user_id": admin["id"], "requested_by": "admin", "environment_snapshot": environment, "ticket_snapshot": ticket})
    return task, ticket, environment


def test_users_pagination_count_and_search_are_not_truncated(enterprise):
    client = enterprise[0]
    create_user(client, "reviewer_a")
    create_user(client, "reviewer_b")
    result = client.get("/api/users", params={"q": "reviewer_", "limit": 1, "offset": 1}).json()
    assert result["total"] == 2 and len(result["items"]) == 1 and result["offset"] == 1
    assert "password_hash" not in json.dumps(result) and "mock-reviewer-password" not in json.dumps(result)


def test_account_disable_revokes_existing_session_and_lists_disabled(enterprise):
    client, store, settings, _ = enterprise
    user = create_user(client)
    with TestClient(create_app(settings)) as second:
        assert second.post("/api/session", json={"username": "reviewer", "password": "mock-reviewer-password"}).status_code == 200
        assert client.patch("/api/users/" + user["id"], json={"disabled": True}).status_code == 200
        assert second.get("/api/session").status_code == 401
    result = client.get("/api/users", params={"disabled": True}).json()
    assert result["total"] == 1 and result["items"][0]["disabled"] is True


def test_password_reset_revokes_tokens_and_does_not_enter_audit(enterprise):
    client, store, settings, _ = enterprise
    user = create_user(client)
    with TestClient(create_app(settings)) as second:
        second.post("/api/session", json={"username": "reviewer", "password": "mock-reviewer-password"})
        new_password = "mock-reset-password-no-logs"
        result = client.post("/api/users/" + user["id"] + "/password", json={"password": new_password})
        assert result.status_code == 200
        assert second.get("/api/session").status_code == 401
    audit = client.get("/api/audit").json()
    assert any(a["action"] == "reset_password" for a in audit["items"])
    assert new_password not in json.dumps(audit)


@pytest.mark.parametrize("patch", [{"disabled": True}, {"role": "analyst"}, {}, {"role": None}, {"password": "not-a-field"}])
def test_current_admin_cannot_disable_demote_or_send_invalid_edit(enterprise, patch):
    client, _, _, admin = enterprise
    assert client.patch("/api/users/" + admin["id"], json=patch).status_code in (409, 422)


@pytest.mark.parametrize("path", ["/api/audit", "/api/system/status"])
def test_enterprise_admin_data_is_not_accessible_to_reviewer(enterprise, path):
    client, _, settings, _ = enterprise
    create_user(client)
    with TestClient(create_app(settings)) as second:
        second.post("/api/session", json={"username": "reviewer", "password": "mock-reviewer-password"})
        assert second.get(path).status_code == 403


def test_request_id_correlates_mutation_and_audit(enterprise):
    client = enterprise[0]
    client.headers["X-Request-ID"] = "enterprise-correlation-123"
    user = create_user(client)
    result = client.get("/api/audit", params={"q": "enterprise-correlation-123", "limit": 1}).json()
    assert result["total"] >= 1 and result["items"][0]["request_id"] == "enterprise-correlation-123"
    assert result["items"][0]["created_at"]
    assert client.get("/api/users").headers["X-Request-ID"] == "enterprise-correlation-123"


@pytest.mark.parametrize("kwargs,status", [({"json": {"username": "bad"}}, 422), ({"json": {}, "headers": {"Origin": "https://untrusted.test"}}, 403)])
def test_validation_and_boundary_errors_have_safe_request_id(enterprise, kwargs, status):
    response = enterprise[0].post("/api/users", **kwargs)
    assert response.status_code == status
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_metrics_requires_dedicated_token_and_has_no_business_labels(enterprise):
    client, store, settings, admin = enterprise
    seed_job(store, admin)
    assert client.get("/metrics").status_code == 403
    client.get("/api/tickets/" + "a" * 32)
    response = client.get("/metrics", headers={"Authorization": "Bearer " + settings.metrics_token})
    assert response.status_code == 200 and "erp_support_http_requests_total" in response.text
    assert 'erp_support_diagnosis_tasks{state="queued"} 1.0' in response.text
    assert "a" * 32 not in response.text and "mock-admin-password" not in response.text


def test_liveness_and_dependency_readiness_are_distinct(enterprise, monkeypatch):
    client, store, _, _ = enterprise
    monkeypatch.setattr(store, "readiness", lambda: {"ready": False})
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json()["ready"] is False


def test_duplicate_worker_message_does_not_repeat_diagnosis(enterprise):
    _, store, settings, admin = enterprise
    task, ticket, _ = seed_job(store, admin)
    calls = []
    def fake(*args):
        calls.append("called")
        args[5]({"status": "success", "tool_name": "mock_only"})
        return {"mode": "test_mock_only", "missing": [], "metrics": {"tool_success_rate": 1}}
    assert jobs.run_task(settings, store, task["id"], diagnostic=fake)
    assert not jobs.run_task(settings, store, task["id"], diagnostic=fake)
    assert calls == ["called"] and len(store.get("task", task["id"])["steps"]) == 1
    assert store.get("ticket", ticket["id"])["status"] == "awaiting_action"


def test_permission_revoked_after_enqueue_stops_all_erp_calls(enterprise):
    _, store, settings, admin = enterprise
    task, ticket, environment = seed_job(store, admin)
    store.update("user", admin["id"], lambda u: u.update(disabled=True))
    calls = []
    assert not jobs.run_task(settings, store, task["id"], diagnostic=lambda *args: calls.append(args))
    assert calls == [] and store.get("task", task["id"])["state"] == "failed"


def test_broker_publish_failure_retains_durable_task_for_outbox(enterprise, monkeypatch):
    _, store, settings, admin = enterprise
    task, _, _ = seed_job(store, admin)
    def unavailable(*args):
        raise RuntimeError("mock-broker-secret-that-must-not-be-stored")
    monkeypatch.setattr(jobs, "client", unavailable)
    assert not jobs.dispatch(settings, store, task["id"])
    queued = store.queued_tasks()
    assert len(queued) == 1 and queued[0]["state"] == "queued"
    assert queued[0]["publish_error"] and "mock-broker-secret" not in json.dumps(queued)


def test_invalid_message_id_is_not_a_database_or_tool_query(enterprise):
    _, store, settings, _ = enterprise
    assert not jobs.run_task(settings, store, "../../production")


def test_worker_readiness_uses_recent_persisted_heartbeat(enterprise):
    _, store, _, _ = enterprise
    assert not jobs.worker_ready(store)
    worker = store.add("worker", {"heartbeat_at": datetime.now(timezone.utc).isoformat()})
    assert jobs.worker_ready(store)
    store.update("worker", worker["id"], lambda w: w.update(heartbeat_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()))
    assert not jobs.worker_ready(store)


@pytest.mark.parametrize("values", [{"queue_backend": "arbitrary"}, {"queue_backend": "celery"}, {"task_capacity": 101}, {"session_hours": 100}, {"metrics_token": "short"}])
def test_enterprise_settings_fail_closed(tmp_path, values):
    with pytest.raises(ValueError):
        Settings(tmp_path, ["http://127.0.0.1:8090"], **values)
