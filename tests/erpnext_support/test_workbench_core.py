"""Unit tests: actual local storage/auth/validation, no ERP or model calls."""

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from holmes.erpnext_support.models import CaseInput, EnvironmentInput, TicketInput
from holmes.erpnext_support.security import password_hash, sanitize, token_hash, verify_password
from holmes.erpnext_support.settings import Settings, origin
from holmes.erpnext_support.storage import Store


def test_password_hash_never_contains_password_and_rejects_wrong_password():
    password = "runtime-only-test-credential"
    encoded = password_hash(password)
    assert password not in encoded
    assert verify_password(password, encoded)
    assert not verify_password(password + "x", encoded)
    assert not verify_password(password, "invalid")
    assert password_hash(password) != encoded


def test_evidence_redacts_credential_email_and_phone_recursively():
    secret = "test-api-key-never-real"
    result = sanitize({"logs": ["phone=13812345678 email=user@example.test token=" + secret], "api_secret": secret}, [secret])
    output = json.dumps(result)
    assert secret not in output and "13812345678" not in output and "user@example.test" not in output


@pytest.mark.parametrize("url", ["file:///tmp/a", "http://169.254.169.254", "http://user:pass@127.0.0.1:9000", "https://erp.example.test/private", "https://erp.example.test?x=1"])
def test_unapproved_origin_shapes_rejected(url):
    with pytest.raises(ValueError):
        origin(url)


def test_local_origin_and_customer_https_origin_are_normalized():
    assert origin("http://127.0.0.1:8766/") == "http://127.0.0.1:8766"
    assert origin("https://erp.example.test") == "https://erp.example.test"


@pytest.mark.parametrize("values", [{"max_rounds": 100}, {"max_rows": 1000}, {"total_seconds": 9999}, {"tool_timeout_seconds": 0}, {"model": "configured-but-no-target"}])
def test_diagnostic_budget_cannot_be_unbounded(tmp_path, values):
    with pytest.raises(ValueError):
        Settings(tmp_path, ["http://127.0.0.1:8766"], **values)


def test_credential_alias_cannot_escape_runtime(tmp_path):
    settings = Settings(tmp_path, ["http://127.0.0.1:8766"])
    with pytest.raises(ValueError):
        settings.credentials("../outside")
    directory = tmp_path / "credentials"
    directory.mkdir()
    (directory / "lab.json").write_text(json.dumps({"api_key": "fake-test-key", "api_secret": "fake-test-secret"}), encoding="utf-8")
    assert settings.credentials("lab")["api_key"] == "fake-test-key"
    (directory / "lab.json").write_text(json.dumps({"api_key": "fake-test-key", "api_secret": "fake-test-secret", "extra": "refused"}), encoding="utf-8")
    with pytest.raises(ValueError):
        settings.credentials("lab")


def test_ticket_requires_timezone_and_rejects_injected_fields():
    values = {"environment_id": "a" * 32, "delivery_note": "DN-TEST-001", "description": "发货提交提示库存不足", "occurred_at": "2026-10-05T14:00:00+08:00"}
    assert TicketInput(**values).occurred_at.tzinfo
    with pytest.raises(ValidationError):
        TicketInput(**{**values, "occurred_at": "2026-10-05T14:00:00"})
    with pytest.raises(ValidationError):
        TicketInput(**{**values, "status": "closed", "submitter": "Administrator"})


def test_case_requires_evidence_conditions_verification_and_source():
    values = {"title": "库存不足诊断规则", "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "symptom": "发货单提交提示库存不足",
              "evidence_requirements": ["历史库存权威检查"], "conditions": ["库存小于请求出库量"], "steps": ["人工核查补货"],
              "verification": ["重新查询并核对发货单状态"], "source_url": "https://github.com/frappe/erpnext"}
    assert CaseInput(**values)
    with pytest.raises(ValidationError):
        CaseInput(**{**values, "conditions": []})
    with pytest.raises(ValidationError):
        CaseInput(**{**values, "status": "approved"})


def test_sqlite_persistence_and_workbench_only_backup(tmp_path):
    first = Store(tmp_path / "support.sqlite3")
    ticket = first.add("ticket", {"description": "用户问题", "status": "pending"}, "env")
    first.audit("admin", "create_ticket", ticket["id"])
    reopened = Store(first.path)
    assert reopened.get("ticket", ticket["id"])["status"] == "pending"
    backup = tmp_path / "backup.sqlite3"
    reopened.backup(backup)
    restored = Store(backup)
    assert restored.get("ticket", ticket["id"])["description"] == "用户问题"
    with pytest.raises(FileExistsError):
        reopened.backup(backup)
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1


def test_restart_marks_running_and_queued_interrupted_preserving_steps(tmp_path):
    store = Store(tmp_path / "support.sqlite3")
    ticket = store.add("ticket", {"status": "investigating"})
    active = store.add("task", {"ticket_id": ticket["id"], "state": "running", "steps": [{"evidence_id": "E001"}]}, parent_id=ticket["id"])
    queued = store.add("task", {"ticket_id": ticket["id"], "state": "queued", "steps": []}, parent_id=ticket["id"])
    completed = store.add("task", {"ticket_id": ticket["id"], "state": "completed", "steps": []}, parent_id=ticket["id"])
    assert store.recover_interrupted() == 2
    assert store.get("task", active["id"])["steps"] == [{"evidence_id": "E001"}]
    assert store.get("task", queued["id"])["state"] == "interrupted"
    assert store.get("task", completed["id"])["state"] == "completed"
    assert store.get("ticket", ticket["id"])["status"] == "needs_information"


def test_evidence_retention_clears_payload_but_retains_task_audit(tmp_path):
    store = Store(tmp_path / "support.sqlite3")
    task = store.add("task", {"state": "completed", "steps": [{"secret": "sanitized"}], "result": {"evidence": ["sanitized evidence"]}})
    store.update("task", task["id"], lambda t: t.update(created_at="2020-01-01T00:00:00+00:00"))
    assert store.expire_evidence(30) == 1
    result = store.get("task", task["id"])
    assert result["result"] is None and result["steps"] == [] and result["evidence_expired"]
    assert result["state"] == "completed"


def test_storage_sql_metacharacters_do_not_change_query_scope(tmp_path):
    store = Store(tmp_path / "support.sqlite3")
    record = store.add("ticket", {"description": "正常记录"})
    assert store.get("ticket", "' OR 1=1 --") is None
    assert store.get("ticket", record["id"])["description"] == "正常记录"


def test_session_storage_uses_hash_instead_of_raw_token():
    token = "opaque-random-test-session"
    assert token not in token_hash(token)
