"""Opt-in live PostgreSQL least-privilege tests against the isolated store DB."""

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError

from holmes.erpnext_support.persistence.migrate import grant_application, initialize_database
from holmes.erpnext_support.persistence.schema import audit_events
from holmes.erpnext_support.storage import Store


@pytest.fixture
def app_store():
    path = os.environ.get("ERP_SUPPORT_STORE_TEST_URL_FILE")
    owner = os.environ.get("ERP_SUPPORT_STORE_MIGRATION_URL_FILE")
    if not path or not owner:
        pytest.skip("Independent PostgreSQL credentials were not explicitly supplied")
    migration = Path(owner).read_text(encoding="utf-8").strip()
    initialize_database(migration)
    grant_application(migration)
    store = Store(Path(path).read_text(encoding="utf-8").strip(), initialize=False)
    yield store
    store.close()


def test_application_role_has_no_superuser_create_database_or_schema_ddl(app_store):
    with app_store.transaction() as db:
        role = db.exec_driver_sql("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user").first()
        assert tuple(role) == (False, False, False)
    with pytest.raises(DBAPIError):
        with app_store.transaction(write=True) as db:
            db.exec_driver_sql('CREATE TABLE "permission_probe_' + uuid.uuid4().hex + '" (id INTEGER)')


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_application_cannot_change_or_delete_persisted_audit_events(app_store, operation):
    request = uuid.uuid4().hex
    app_store.audit("permission-test", "probe", "self-owned", request_id=request)
    audit = app_store.list_audit(filters={"q": request})["items"][0]
    with pytest.raises(DBAPIError):
        with app_store.transaction(write=True) as db:
            statement = update(audit_events).values(outcome="bad") if operation == "update" else delete(audit_events)
            db.execute(statement.where(audit_events.c.id == audit["id"]))
    assert app_store.list_audit(filters={"q": request})["items"][0]["outcome"] == "success"
