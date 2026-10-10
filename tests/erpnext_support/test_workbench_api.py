"""API/state/security tests with labelled mock ERP diagnosis; no real ERP calls."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from holmes.erpnext_support.security import password_hash
from holmes.erpnext_support.server import create_app
from holmes.erpnext_support.settings import Settings
from holmes.erpnext_support.storage import Store


@pytest.fixture
def workbench(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "credentials").mkdir(parents=True)
    (runtime / "credentials" / "lab.json").write_text(json.dumps({"api_key": "mock-api-key", "api_secret": "mock-api-secret"}), encoding="utf-8")
    settings = Settings(runtime, ["http://127.0.0.1:8090"])
    store = Store(settings.database)
    admin = store.add("user", {"username": "admin", "role": "admin", "environment_ids": [], "password_hash": password_hash("test-admin-password")})
    analyst = store.add("user", {"username": "analyst", "role": "analyst", "environment_ids": [], "password_hash": password_hash("test-analyst-password")})

    def fake_diagnosis(settings, environment, ticket, credentials, authorize, on_step, cases):
        assert authorize()
        on_step({"evidence_id": "E001", "status": "success", "tool_name": "mock_business_read"})
        return {"mode": "test_mock_only", "classification": "normal", "facts": [], "causes": [], "missing": [],
                "evidence": [], "manual_steps": [], "verification": [], "limits": [],
                "business_state": {"docstatus": 1, "business_complete": True}, "metrics": {"tool_success_rate": 1}}

    app = create_app(settings, diagnostic=fake_diagnosis, connection_check=lambda env, creds: {
        "version_supported": True, "read_permissions": {"Delivery Note": True}, "missing": [], "source": "mock_only"})
    with TestClient(app) as client:
        yield client, store, settings, admin, analyst


def sign_in(client, name="admin", password="test-admin-password"):
    response = client.post("/api/session", json={"username": name, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


def environment(client, name="隔离ERP测试"):
    response = client.post("/api/environments", json={"name": name, "base_url": "http://127.0.0.1:8090", "credential_ref": "lab",
        "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "installed_apps": ["frappe", "erpnext"],
        "allowed_services": ["application"]})
    assert response.status_code == 200
    return response.json()


def ticket(client, env):
    response = client.post("/api/tickets", json={"environment_id": env["id"], "delivery_note": "DN-TEST-001",
        "description": "发货提交时出现库存错误", "occurred_at": "2026-10-05T15:00:00+08:00"})
    assert response.status_code == 200
    return response.json()


def wait_task(client, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        detail = client.get("/api/tickets/" + identifier).json()
        if detail["tasks"] and detail["tasks"][0]["state"] not in ("queued", "running"):
            return detail
        time.sleep(0.02)
    pytest.fail("Mock state task did not finish in budget")


def case_values():
    return {"title": "标准库存历史时点排查", "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "symptom": "当前库存够但过账时点不足",
        "evidence_requirements": ["原生历史库存查询"], "conditions": ["过账时间早于实际入库"], "steps": ["授权人员核查正确业务时点"],
        "verification": ["重查单据及对应出库流水"], "source_url": "https://github.com/frappe/erpnext"}


@pytest.mark.parametrize("path", ["/api/session", "/api/tickets", "/api/environments", "/api/cases", "/api/users"])
def test_login_required_for_workbench_data(workbench, path):
    assert workbench[0].get(path).status_code == 401


def test_cookie_is_http_only_and_session_never_returns_password(workbench):
    client = workbench[0]
    response = client.post("/api/session", json={"username": "admin", "password": "test-admin-password"})
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    assert "password" not in response.text
    assert client.get("/api/session").json()["user"]["username"] == "admin"


def test_mutation_needs_csrf_and_untrusted_origin_is_rejected(workbench):
    client = workbench[0]
    sign_in(client)
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/environments", json={}).status_code == 403
    assert client.post("/api/session", json={"username": "admin", "password": "test-admin-password"}, headers={"Origin": "https://attacker.example"}).status_code == 403


def test_validation_does_not_echo_password_or_large_request(workbench):
    client = workbench[0]
    sensitive = "SENSITIVE-INPUT-" * 30
    response = client.post("/api/session", json={"username": "admin", "password": sensitive})
    assert response.status_code == 422 and sensitive not in response.text
    assert client.post("/api/session", content=b"x" * 65537, headers={"Content-Type": "application/json"}).status_code == 413


def test_admin_environment_whitelist_and_secret_alias_boundary(workbench):
    client = workbench[0]
    sign_in(client)
    values = {"name": "bad", "base_url": "http://127.0.0.1:9999", "credential_ref": "lab", "erpnext_version": "15.85.1", "frappe_version": "15.87.0"}
    assert client.post("/api/environments", json=values).status_code == 403
    assert client.post("/api/environments", json={**values, "base_url": "http://127.0.0.1:8090", "credential_ref": "../../secret"}).status_code == 422
    env = environment(client)
    assert "api_key" not in client.get("/api/environments").text
    assert client.post("/api/environments/" + env["id"] + "/check").json()["source"] == "mock_only"


def test_cross_environment_read_task_and_configuration_are_denied(workbench):
    client, store, _, _, analyst = workbench
    sign_in(client)
    first, second = environment(client, "授权环境"), environment(client, "未授权环境")
    foreign_ticket = ticket(client, second)
    store.update("user", analyst["id"], lambda u: u.update(environment_ids=[first["id"]]))
    sign_in(client, "analyst", "test-analyst-password")
    assert len(client.get("/api/environments").json()["items"]) == 1
    assert client.get("/api/tickets/" + foreign_ticket["id"]).status_code == 403
    assert client.post("/api/tickets/" + foreign_ticket["id"] + "/diagnose").status_code == 403
    assert client.post("/api/environments/" + second["id"] + "/check").status_code == 403
    assert client.post("/api/environments", json={}).status_code in (403, 422)


def test_mock_diagnosis_human_action_review_and_manual_closure(workbench):
    client = workbench[0]
    sign_in(client)
    obj = ticket(client, environment(client))
    assert obj["status"] == "pending"
    assert client.post("/api/tickets/" + obj["id"] + "/review").status_code == 409
    assert client.post("/api/tickets/" + obj["id"] + "/diagnose").status_code == 202
    result = wait_task(client, obj["id"])
    assert result["tasks"][0]["steps"][0]["evidence_id"] == "E001"
    assert result["status"] == "awaiting_action"
    assert client.post("/api/tickets/" + obj["id"] + "/actions", json={"description": "人工已在隔离ERP确认实际处理完成"}).status_code == 200
    assert client.post("/api/tickets/" + obj["id"] + "/review").status_code == 202
    result = wait_task(client, obj["id"])
    review = result["reviews"][0]
    assert result["status"] == "awaiting_review" and review["result"]["review"]["requires_human_confirmation"]
    assert client.post("/api/tickets/" + obj["id"] + "/close", json={"review_id": review["id"], "confirmation": "已人工核对实际发货完成"}).json()["status"] == "closed"
    assert client.post("/api/tickets/" + obj["id"] + "/diagnose").status_code == 409


def test_only_latest_same_ticket_review_can_close(workbench):
    client, store, *_ = workbench
    sign_in(client)
    obj = ticket(client, environment(client))
    wrong = store.add("task", {"ticket_id": "another", "kind": "review", "state": "completed", "result": {"review": {"business_complete": True}}}, parent_id="another")
    assert client.post("/api/tickets/" + obj["id"] + "/close", json={"review_id": wrong["id"], "confirmation": "人工确认完成"}).status_code == 409


def test_case_requires_human_review_and_edit_revokes_approval(workbench):
    client = workbench[0]
    sign_in(client)
    obj = client.post("/api/cases", json=case_values()).json()
    assert obj["status"] == "draft"
    response = client.post("/api/cases/" + obj["id"] + "/approve", json={"decision": "approved", "note": "已核对官方版本与必要证据"})
    assert response.json()["status"] == "approved"
    assert client.put("/api/cases/" + obj["id"], json=case_values()).json()["status"] == "draft"
    sign_in(client, "analyst", "test-analyst-password")
    assert client.post("/api/cases/" + obj["id"] + "/approve", json={"decision": "approved", "note": "不能审核"}).status_code == 403


def test_analyst_edit_cannot_override_concurrent_case_review(workbench, monkeypatch):
    client, store, *_ = workbench
    sign_in(client, "analyst", "test-analyst-password")
    case = client.post("/api/cases", json=case_values()).json()
    request_store = client.app.state.store
    original_get = request_store.get
    changed = False

    def reviewed_after_read(kind, identifier):
        nonlocal changed
        value = original_get(kind, identifier)
        if kind == "case" and identifier == case["id"] and not changed:
            changed = True
            store.update("case", identifier, lambda row: row.update(status="approved", approved_by="admin"))
        return value

    # A second request commits its approval between the read and locked update.
    monkeypatch.setattr(request_store, "get", reviewed_after_read)
    assert client.put("/api/cases/" + case["id"], json=case_values()).status_code == 403
    assert original_get("case", case["id"])["status"] == "approved"
    assert original_get("case", case["id"])["approved_by"] == "admin"


def test_missing_credentials_gives_explicit_setup_error(workbench):
    client = workbench[0]
    sign_in(client)
    env = environment(client)
    workbench[1].update("environment", env["id"], lambda e: e.update(credential_ref="missing"))
    assert client.post("/api/environments/" + env["id"] + "/check").status_code == 422


def test_environment_with_tickets_cannot_be_deleted(workbench):
    client = workbench[0]
    sign_in(client)
    env = environment(client)
    ticket(client, env)
    assert client.delete("/api/environments/" + env["id"]).status_code == 409


def test_ticket_state_and_environment_cannot_be_changed_by_patch(workbench):
    client = workbench[0]
    sign_in(client)
    obj = ticket(client, environment(client))
    assert client.patch("/api/tickets/" + obj["id"], json={"status": "closed", "environment_id": "b" * 32}).status_code == 422


def test_logout_invalidates_server_session(workbench):
    client = workbench[0]
    sign_in(client)
    assert client.delete("/api/session").status_code == 200
    assert client.get("/api/tickets").status_code == 401


@pytest.mark.parametrize("operation", ["patch_ticket", "diagnose", "action", "create_case", "edit_case"])
def test_unknown_read_only_role_cannot_mutate(workbench, operation):
    client, store, settings, admin, analyst = workbench
    sign_in(client)
    env = environment(client)
    obj = ticket(client, env)
    case = client.post("/api/cases", json=case_values()).json()
    store.update("user", admin["id"], lambda u: u.update(role="viewer", environment_ids=[env["id"]]))
    # Enterprise permissions revoke old sessions. Authenticate again so this
    # assertion tests the role gate independently of session invalidation.
    sign_in(client)
    if operation == "patch_ticket":
        response = client.patch("/api/tickets/" + obj["id"], json={"description": "读取角色不可以修改"})
    elif operation == "diagnose":
        response = client.post("/api/tickets/" + obj["id"] + "/diagnose")
    elif operation == "action":
        response = client.post("/api/tickets/" + obj["id"] + "/actions", json={"description": "读取角色不可以写记录"})
    elif operation == "create_case":
        response = client.post("/api/cases", json=case_values())
    else:
        response = client.put("/api/cases/" + case["id"], json=case_values())
    assert response.status_code == 403
