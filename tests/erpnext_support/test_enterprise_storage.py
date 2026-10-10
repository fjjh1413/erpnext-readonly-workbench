"""Real SQLite transactions and opt-in isolated PostgreSQL; no database mocks."""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, update
from sqlalchemy.engine import make_url

from holmes.erpnext_support.observability import request_id_context
from holmes.erpnext_support.persistence.migrate import grant_application, initialize_database
from holmes.erpnext_support.persistence.schema import job_leases, records
from holmes.erpnext_support.storage import AdminInvariant, Store, TaskConflict, UserUnique, VersionConflict


@pytest.fixture(scope="session")
def postgres_target():
    path = os.environ.get("ERP_SUPPORT_STORE_TEST_URL_FILE")
    owner = os.environ.get("ERP_SUPPORT_STORE_MIGRATION_URL_FILE")
    if not path or not owner:
        pytest.skip("Independent PostgreSQL credentials were not explicitly supplied")
    location = Path(path).read_text(encoding="utf-8").strip()
    migration = Path(owner).read_text(encoding="utf-8").strip()
    if not location.startswith("postgresql+pg8000://") or not migration.startswith("postgresql+pg8000://"):
        pytest.fail("Expected controlled PostgreSQL test DSN files")
    target = make_url(location)
    if target.host not in ("127.0.0.1", "localhost") or target.database != "enterprise_store_test" or target.username != "workbench_app":
        pytest.fail("Refusing a database outside the explicitly owned store test lab")
    initialize_database(migration)
    grant_application(migration)
    return location


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "postgresql":
        obj = Store(request.getfixturevalue("postgres_target"), initialize=False)
        # Only this explicitly named, independently created test database is
        # reset. No ERP, workbench API database, or immutable audit is changed.
        with obj.transaction(write=True) as db:
            db.execute(delete(records))
    else:
        path = tmp_path / "enterprise.sqlite3"
        initialize_database(path)
        obj = Store(path)
    yield obj
    obj.close()


def ticket(store, **values):
    return store.add("ticket", {"status": "pending", "description": "contract item", **values})


def task(store, **values):
    note = ticket(store)
    return note, store.create_task(note["id"], {"kind": "diagnosis", "steps": [], **values}, active_limit=100)


def test_json_identity_version_and_rollback(store):
    obj = ticket(store)
    assert store.record_version("ticket", obj["id"]) == 1
    store.update("ticket", obj["id"], lambda value: value.update(description="changed"), expected_version=1)
    assert store.record_version("ticket", obj["id"]) == 2
    with pytest.raises(VersionConflict):
        store.update("ticket", obj["id"], lambda value: value.update(status="bad"), expected_version=1)
    with pytest.raises(ValueError):
        store.update("ticket", obj["id"], lambda value: value.update(id="other"))
    assert store.get("ticket", obj["id"])["status"] == "pending"


def test_parallel_mutations_preserve_every_increment(store):
    obj = ticket(store, count=0)
    def increment(_):
        store.update("ticket", obj["id"], lambda value: value.update(count=value["count"] + 1))
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(increment, range(36)))
    assert store.get("ticket", obj["id"])["count"] == 36
    assert store.record_version("ticket", obj["id"]) == 37


def test_parallel_enqueue_has_one_task_and_one_ticket_transition(store):
    note = ticket(store)
    def enqueue(_):
        try:
            return store.create_task(note["id"], {"kind": "diagnosis", "steps": []}, active_limit=100)
        except TaskConflict:
            return None
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(enqueue, range(8)))
    assert len([value for value in results if value]) == 1
    assert len(store.list("task", note["id"])) == 1
    assert store.ticket_snapshot(note["id"])["status"] == "investigating"


def test_duplicate_message_claim_and_wrong_owner_are_fenced(store):
    note, item = task(store)
    claimed = store.claim_task(item["id"], "worker-one", 60)
    assert claimed["state"] == "running" and claimed["worker_id"] == "worker-one"
    assert store.claim_task(item["id"], "worker-two", 60) is None
    assert store.heartbeat_task(item["id"], "worker-two") is False
    assert store.step_task(item["id"], "worker-two", {"bad": True}) is False
    assert store.finish_task(item["id"], note["id"], {"state": "completed", "result": {"bad": True}}, "closed", worker_id="worker-two") is False
    assert store.get("task", item["id"])["steps"] == []
    assert store.finish_task(item["id"], note["id"], {"state": "completed"}, "closed") is False


def test_heartbeat_step_and_finish_publish_atomic_snapshot(store):
    note, item = task(store)
    store.claim_task(item["id"], "owner")
    assert store.heartbeat_task(item["id"], "owner")
    assert store.step_task(item["id"], "owner", {"evidence_id": "E001"})
    assert store.finish_task(item["id"], note["id"], {"state": "completed", "result": {"status": "needs_information"}}, "needs_information", worker_id="owner")
    snapshot = store.ticket_snapshot(note["id"])
    assert snapshot["status"] == "needs_information"
    assert snapshot["tasks"][0]["state"] == "completed"
    assert snapshot["tasks"][0]["steps"] == [{"evidence_id": "E001"}]
    assert not store.finish_task(item["id"], note["id"], {"state": "failed"}, "pending", worker_id="owner")


def test_expired_lease_preserves_steps_and_does_not_stop_other_workers_or_queue(store):
    note, first = task(store)
    _, second = task(store)
    _, queued = task(store)
    store.claim_task(first["id"], "expired")
    store.claim_task(second["id"], "live")
    store.step_task(first["id"], "expired", {"saved": True})
    with store.transaction(write=True) as db:
        db.execute(update(job_leases).where(job_leases.c.task_id == first["id"]).values(lease_until=0))
    assert not store.heartbeat_task(first["id"], "expired")
    assert not store.step_task(first["id"], "expired", {"late": True})
    assert not store.finish_task(first["id"], note["id"], {"state": "completed"}, "closed", worker_id="expired")
    assert store.reconcile_expired() == 1
    assert store.get("task", first["id"])["steps"] == [{"saved": True}]
    assert store.get("task", second["id"])["state"] == "running"
    assert store.get("task", queued["id"])["state"] == "queued"


def test_outbox_failure_is_safe_and_queued_survives_store_restart(store):
    _, item = task(store)
    assert store.dispatched_task(item["id"], publish_error="password=do-not-save")
    updated = store.get("task", item["id"])
    assert updated["publish_error"] == "queue_publish_failed"
    assert "do-not-save" not in json.dumps(updated)
    assert any(value["id"] == item["id"] for value in store.queued_tasks(limit=200))
    location = store.path if store.dialect == "sqlite" else Path(os.environ["ERP_SUPPORT_STORE_TEST_URL_FILE"]).read_text(encoding="utf-8").strip()
    reopened = Store(location, initialize=False)
    try:
        assert any(value["id"] == item["id"] for value in reopened.queued_tasks(limit=200))
        assert reopened.get("task", item["id"])["state"] == "queued"
    finally:
        reopened.close()


def test_session_account_disabling_revokes_before_reenable(store):
    user = store.create_user_unique({"username": "usr" + os.urandom(6).hex(), "role": "analyst", "environment_ids": []})
    digest = hashlib.sha256(b"only-hashed-tokens").hexdigest()
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.create_session(digest, user["id"], "csrf", expiry)
    assert store.get_session(digest)["user_id"] == user["id"]
    store.update_user_admin_guard(user["id"], {"disabled": True})
    assert store.get_session(digest) is None
    store.update_user_admin_guard(user["id"], {"disabled": False})
    assert store.get_session(digest) is None


def test_user_name_unique_and_admin_guard_are_transactional(store):
    name = "adm" + os.urandom(6).hex()
    user = store.create_user_unique({"username": name, "role": "admin", "environment_ids": []})
    with pytest.raises(UserUnique):
        store.create_user_unique({"username": name.upper(), "role": "analyst"})
    assert store.get_by_username(name.upper())["id"] == user["id"]
    with pytest.raises(AdminInvariant):
        store.update_user_admin_guard(user["id"], {"disabled": True})
    second = store.create_user_unique({"username": name + "b", "role": "admin"})
    def disable(identifier):
        try:
            store.update_user_admin_guard(identifier, {"disabled": True})
            return True
        except AdminInvariant:
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(workers.map(disable, [user["id"], second["id"]])) == 1


def test_pagination_filters_before_limit_and_empty_environment_means_none(store):
    environment = store.add("environment", {"name": "scope" + os.urandom(6).hex()})
    for number in range(7):
        store.add("ticket", {"description": f"case_{number}", "status": "pending" if number % 2 else "closed"}, environment_id=environment["id"])
    page = store.page("ticket", limit=2, offset=1, filters={"status": "pending", "q": "case_"}, environment_ids=[environment["id"]])
    assert page["total"] == 3 and len(page["items"]) == 2
    assert store.page("ticket", environment_ids=[])["total"] == 0
    assert store.page("environment", environment_ids=[environment["id"]])["total"] == 1
    assert store.page("ticket", filters={"q": "%_"}, environment_ids=[environment["id"]])["total"] == 0
    with pytest.raises(ValueError):
        store.page("ticket", filters={"sql": "DROP TABLE records"})


def test_audit_filters_correlation_and_detail_redaction(store):
    request = "audit" + os.urandom(6).hex()
    marker = request_id_context.set(request)
    try:
        store.audit("actor", "login", "user-id", resource_kind="user", outcome="denied", detail={"password": "do-not-save", "contact": "联系13800138000"})
    finally:
        request_id_context.reset(marker)
    result = store.list_audit(filters={"q": request}, limit=1)
    assert result["total"] == 1 and result["items"][0]["created_at"] == result["items"][0]["at"]
    assert result["items"][0]["request_id"] == request
    assert "do-not-save" not in json.dumps(result) and "13800138000" not in json.dumps(result)
    assert store.readiness()["ready"]


def test_wrong_ticket_cannot_publish_task_completion(store):
    _, item = task(store)
    other = ticket(store)
    store.claim_task(item["id"], "owner")
    assert not store.finish_task(item["id"], other["id"], {"state": "completed"}, "closed", worker_id="owner")
    assert store.get("ticket", other["id"])["status"] == "pending"


def test_enqueue_uses_locked_current_ticket_snapshot(store):
    note = ticket(store, description="old")
    store.update("ticket", note["id"], lambda value: value.update(description="new"))
    item = store.create_task(note["id"], {"kind": "diagnosis", "ticket_snapshot": note}, active_limit=100)
    assert item["ticket_snapshot"]["description"] == "new"


def test_action_and_close_use_locked_latest_review_and_handling_order(store):
    note = ticket(store)
    action = store.add_action(note["id"], {"description": "performed by human"})
    assert store.get("ticket", note["id"])["status"] == "awaiting_review"
    review = store.add("task", {"ticket_id": note["id"], "kind": "review", "state": "completed", "result": {"review": {"business_complete": True}}}, parent_id=note["id"])
    with pytest.raises(TaskConflict):
        store.close_ticket(note["id"], "wrong", "confirmed", "admin")
    result = store.close_ticket(note["id"], review["id"], "human confirmed shipment", "admin")
    assert result["status"] == "closed"
    with pytest.raises(TaskConflict):
        store.add_action(note["id"], {"description": "late"})


def test_new_action_invalidates_previous_review_and_override_requires_description(store):
    note = ticket(store)
    store.add_action(note["id"], {"description": "human action"})
    review = store.add("task", {"ticket_id": note["id"], "kind": "review", "state": "partial", "result": {"review": {"business_complete": False}}}, parent_id=note["id"])
    with pytest.raises(TaskConflict):
        store.close_ticket(note["id"], review["id"], "short", "admin", manual_override=True)
    store.add_action(note["id"], {"description": "new human action"})
    with pytest.raises(TaskConflict):
        store.close_ticket(note["id"], review["id"], "reason " * 8, "admin", manual_override=True)


def test_environment_delete_checks_all_references_after_two_hundred_rows(store):
    env = store.add("environment", {"name": "delete probe"})
    reference = store.add("ticket", {"status": "pending"}, environment_id=env["id"])
    for index in range(201):
        store.add("ticket", {"status": "pending", "index": index})
    assert reference["id"] not in {item["id"] for item in store.list("ticket", limit=200)}
    with pytest.raises(TaskConflict):
        store.delete_environment_guarded(env["id"])
    assert store.get("environment", env["id"])
    empty = store.add("environment", {"name": "empty"})
    store.delete_environment_guarded(empty["id"])
    with pytest.raises(KeyError):
        store.add("ticket", {"status": "pending"}, environment_id=empty["id"])


def test_backup_reservation_is_exclusive_under_two_callers(store, tmp_path):
    target = tmp_path / "fresh-backup.sqlite3"
    if store.dialect == "postgresql":
        with pytest.raises(RuntimeError):
            store.backup(target)
        assert not target.exists()
        return
    saved = ticket(store, description="preserved")
    def backup(_):
        try:
            store.backup(target)
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(workers.map(backup, [1, 2])) == 1
    restored = Store(target)
    assert restored.get("ticket", saved["id"])["description"] == "preserved"
    restored.close()


def test_ticket_snapshot_reads_only_bounded_children_and_retains_latest_result(store):
    note = ticket(store)
    with store.transaction(write=True) as db:
        for index in range(210):
            store._insert(db, "task", {"ticket_id": note["id"], "kind": "diagnosis", "state": "completed", "result": {"index": index}}, parent_id=note["id"])
            store._insert(db, "action", {"description": "history", "index": index}, parent_id=note["id"])
    result = store.ticket_snapshot(note["id"])
    assert len(result["tasks"]) == len(result["actions"]) == 200
    assert result["latest_result"] == {"index": 209}
    assert result["id"] == note["id"]
