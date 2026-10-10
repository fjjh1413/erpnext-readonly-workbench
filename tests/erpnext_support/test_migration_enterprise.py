"""Read-only legacy source preservation and real Alembic SQLite migrations."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from holmes.erpnext_support.persistence.migrate import copy_legacy_sqlite, grant_application, initialize_database
from holmes.erpnext_support.storage import MigrationRequired, Store


@pytest.fixture
def legacy(tmp_path):
    source = tmp_path / "original.sqlite3"
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT,environment_id TEXT,parent_id TEXT,created_at TEXT,payload TEXT);"
                        "CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires_at TEXT);"
                        "CREATE TABLE audit(id INTEGER PRIMARY KEY,at TEXT,actor TEXT,action TEXT,target TEXT);")
        for kind, identifier, value in (("user", "1" * 32, {"username": "admin", "role": "admin", "password_hash": "legacy-private-hash"}),
                                        ("ticket", "2" * 32, {"status": "investigating"}),
                                        ("task", "3" * 32, {"ticket_id": "2" * 32, "state": "running", "steps": [{"saved": True}]})):
            value.update(id=identifier, created_at="2026-10-01T00:00:00+00:00")
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?)", (identifier, kind, None, "2" * 32 if kind == "task" else None, value["created_at"], json.dumps(value)))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", ("4" * 64, "1" * 32, "csrf", "2027-01-01T00:00:00+00:00"))
        db.execute("INSERT INTO audit VALUES(1,?,?,?,?)", ("2026-10-01T00:00:00+00:00", "admin", "login", "1" * 32))
    return source


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_constructor_refuses_without_changing_source(legacy):
    before = digest(legacy)
    with pytest.raises(MigrationRequired):
        Store(legacy)
    assert digest(legacy) == before


def test_dry_run_does_not_create_target_or_print_business_data(legacy, tmp_path, capsys):
    before = digest(legacy)
    target = tmp_path / "future.sqlite3"
    result = copy_legacy_sqlite(legacy, target)
    assert result["counts"] == {"records": 3, "sessions": 1, "audit": 1}
    assert not target.exists() and digest(legacy) == before
    assert "legacy-private-hash" not in json.dumps(result) + capsys.readouterr().out


def test_explicit_copy_import_preserves_source_and_interrupts_old_local_jobs(legacy, tmp_path):
    before = digest(legacy)
    target = tmp_path / "new.sqlite3"
    result = copy_legacy_sqlite(legacy, target, dry_run=False)
    assert result["imported_records"] == 3 and digest(legacy) == before
    with Store(target).transaction() as db:
        assert "alembic_version" in inspect(db).get_table_names()
    store = Store(target)
    assert store.get("task", "3" * 32)["state"] == "interrupted"
    assert store.get("task", "3" * 32)["steps"] == [{"saved": True}]
    assert store.get("ticket", "2" * 32)["status"] == "needs_information"
    assert store.get_by_username("admin")["password_hash"] == "legacy-private-hash"
    assert store.get_session("4" * 64) is None
    assert store.list_audit()["total"] == 1
    with pytest.raises(ValueError):
        copy_legacy_sqlite(legacy, target, dry_run=False)
    store.close()


def test_source_and_target_must_differ(legacy):
    before = digest(legacy)
    with pytest.raises(ValueError):
        copy_legacy_sqlite(legacy, legacy, dry_run=False)
    with pytest.raises(ValueError):
        copy_legacy_sqlite(legacy, "sqlite+pysqlite:///" + legacy.as_posix(), dry_run=False)
    assert digest(legacy) == before


def test_unrelated_or_populated_target_is_never_overwritten(legacy, tmp_path):
    target = tmp_path / "populated.sqlite3"
    store = Store(target)
    saved = store.add("ticket", {"description": "saved"})
    with pytest.raises(ValueError):
        copy_legacy_sqlite(legacy, target, dry_run=False)
    assert store.get("ticket", saved["id"])["description"] == "saved"
    store.close()
    with pytest.raises(MigrationRequired):
        initialize_database(legacy)


def test_bad_import_rolls_back_all_rows_and_keeps_source(legacy, tmp_path):
    with sqlite3.connect(legacy) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='user'").fetchone()
        data = json.loads(row[0])
        data["username"] = "invalid name"
        db.execute("UPDATE records SET payload=? WHERE kind='user'", (json.dumps(data),))
    before = digest(legacy)
    target = tmp_path / "rollback.sqlite3"
    with pytest.raises(ValueError):
        copy_legacy_sqlite(legacy, target, dry_run=False)
    store = Store(target)
    assert store.page("ticket")["total"] == 0
    assert store.page("user")["total"] == 0
    assert digest(legacy) == before
    store.close()


def test_late_audit_constraint_failure_rolls_back_inserted_records(legacy, tmp_path):
    with sqlite3.connect(legacy) as db:
        db.execute("UPDATE audit SET at=NULL")
    before = digest(legacy)
    target = tmp_path / "late-rollback.sqlite3"
    with pytest.raises(IntegrityError):
        copy_legacy_sqlite(legacy, target, dry_run=False)
    store = Store(target)
    assert store.page("user")["total"] == store.page("ticket")["total"] == 0
    assert digest(legacy) == before
    store.close()


def test_actual_sqlite_to_postgresql_copy_import_has_preserved_source_and_app_access(legacy):
    target = os.environ.get("ERP_SUPPORT_COPY_TEST_MIGRATION_URL_FILE")
    app = os.environ.get("ERP_SUPPORT_COPY_TEST_URL_FILE")
    if not target or not app:
        pytest.skip("Fresh dedicated PostgreSQL copy-import database was not supplied")
    owner_url = Path(target).read_text(encoding="utf-8").strip()
    app_url = Path(app).read_text(encoding="utf-8").strip()
    before = digest(legacy)
    planned = copy_legacy_sqlite(legacy, owner_url)
    assert planned["dry_run"] and planned["counts"]["records"] == 3
    applied = copy_legacy_sqlite(legacy, owner_url, dry_run=False)
    grant_application(owner_url)
    store = Store(app_url, initialize=False)
    assert store.get_by_username("admin")["id"] == "1" * 32
    assert store.get("task", "3" * 32)["state"] == "interrupted"
    assert store.list_audit()["total"] == 1
    assert digest(legacy) == before and applied["source_preserved"]
    with pytest.raises(ValueError):
        copy_legacy_sqlite(legacy, owner_url, dry_run=False)
    store.close()


def test_alembic_upgrade_is_idempotent_and_role_name_is_guarded(tmp_path):
    target = tmp_path / "alembic.sqlite3"
    initialize_database(target)
    initialize_database(target)
    store = Store(target)
    assert store.readiness()["ready"]
    with pytest.raises(ValueError):
        grant_application(target, 'app; DROP TABLE records')
    store.close()
