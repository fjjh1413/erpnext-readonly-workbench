"""Explicit Alembic initialization and read-only legacy copy import."""

import argparse
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, insert, select
from sqlalchemy.engine import make_url

from holmes.erpnext_support.persistence.schema import SCHEMA_REVISION, audit_events, job_leases, metadata, records, schema_info, sessions, usernames
from holmes.erpnext_support.storage import MigrationRequired, Store, database_engine, utc_now


def initialize_database(location: Path | str) -> None:
    engine, _ = database_engine(location)
    try:
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables and "schema_info" not in tables:
                raise MigrationRequired("Existing tables are preserved; initialize a fresh workbench database")
            cfg = Config()
            cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
            cfg.attributes["connection"] = connection
            if "schema_info" in tables and "alembic_version" not in tables:
                if connection.execute(select(schema_info.c.revision)).scalar_one_or_none() != SCHEMA_REVISION:
                    raise MigrationRequired("Unrecognized workbench schema")
                command.stamp(cfg, SCHEMA_REVISION)
            else:
                command.upgrade(cfg, "head")
    finally:
        engine.dispose()


def grant_application(location: Path | str, app_role: str = "workbench_app") -> None:
    """Run only under the migration owner; never grant schema/database CREATE."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", app_role):
        raise ValueError("Invalid application role name")
    engine, _ = database_engine(location)
    try:
        if engine.dialect.name != "postgresql":
            raise ValueError("Application grants are PostgreSQL-only")
        role = engine.dialect.identifier_preparer.quote_identifier(app_role)
        with engine.begin() as db:
            db.exec_driver_sql(f"REVOKE CREATE ON SCHEMA public FROM {role}")
            db.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            db.exec_driver_sql(f"GRANT SELECT,INSERT,UPDATE,DELETE ON records,sessions,job_leases,usernames TO {role}")
            db.exec_driver_sql(f"REVOKE UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON audit_events FROM {role}")
            db.exec_driver_sql(f"GRANT SELECT,INSERT ON audit_events TO {role}")
            db.exec_driver_sql(f"GRANT SELECT ON schema_info,store_locks,alembic_version TO {role}")
            # PostgreSQL FOR UPDATE also requires UPDATE privilege, even for a
            # singleton locking row whose value is not changed.
            db.exec_driver_sql(f"GRANT UPDATE ON store_locks TO {role}")
            db.exec_driver_sql(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
            # New tables default to read/insert only. Each future reviewed
            # migration must grant writes for its explicit non-audit tables.
            db.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON TABLES FROM {role}")
            db.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT ON TABLES TO {role}")
            db.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {role}")
    finally:
        engine.dispose()


@contextmanager
def legacy_source(path: Path):
    path = path.absolute()
    if not path.is_file() or any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
        raise ValueError("Legacy source must be a real existing file without links")
    with sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Legacy database integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"records", "sessions", "audit"}.issubset(tables):
            raise ValueError("Unrecognized legacy schema")
        if "version" in {row[1] for row in db.execute("PRAGMA table_info(records)")}:
            raise ValueError("Source already uses enterprise storage")
        if path.stat().st_size > 536870912 or db.execute("SELECT COUNT(*) FROM records").fetchone()[0] > 100000:
            raise ValueError("Legacy source exceeds the reviewed migration batch limit")
        yield db


def target_empty(location: Path | str) -> bool:
    if isinstance(location, Path) or "://" not in str(location):
        if not Path(location).exists():
            return True
    else:
        url = make_url(str(location))
        if url.drivername.startswith("sqlite") and url.database and not Path(url.database).exists():
            return True
    engine, path = database_engine(location)
    try:
        with engine.connect() as db:
            tables = set(inspect(db).get_table_names())
            if not tables:
                return True
            if "schema_info" not in tables:
                return False
            if tables - set(metadata.tables) - {"alembic_version"}:
                return False
            return all(db.execute(select(func.count()).select_from(table)).scalar_one() == 0
                       for table in (records, sessions, audit_events, job_leases, usernames))
    finally:
        engine.dispose()


def copy_legacy_sqlite(source: Path, target: Path | str, *, dry_run: bool = True) -> dict:
    """All-or-nothing import into an empty target; source remains read-only.

    Existing login sessions are deliberately not transferred. Active local jobs
    become interrupted with their steps preserved; they are never auto-replayed.
    """
    if isinstance(target, Path) or "://" not in str(target):
        if source.resolve() == Path(target).resolve():
            raise ValueError("Source and target must differ")
    else:
        engine, target_path = database_engine(target)
        engine.dispose()
        if target_path and source.resolve() == target_path.resolve():
            raise ValueError("Source and target must differ")
    if not target_empty(target):
        raise ValueError("Target must be an empty, separately initialized database")
    with legacy_source(source) as db:
        counts = {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("records", "sessions", "audit")}
        summary = {"dry_run": dry_run, "counts": counts, "sessions_reset": counts["sessions"], "source_preserved": True}
        names = set()
        for row in db.execute("SELECT id,kind,payload FROM records"):
            if len(row["payload"].encode("utf-8")) > 8388608:
                raise ValueError("Legacy record exceeds the migration byte limit")
            value = json.loads(row["payload"])
            if not isinstance(value, dict) or value.get("id") != row["id"] or not re.fullmatch(r"[a-f0-9]{32}", row["id"]):
                raise ValueError("Invalid legacy record identity")
            if row["kind"] == "user":
                name = value.get("username", "")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", name) or name.casefold() in names:
                    raise ValueError("Invalid or duplicate legacy account name")
                names.add(name.casefold())
        if dry_run:
            return summary
        initialize_database(target)
        store = Store(target, initialize=False)
        try:
            with store.transaction(write=True) as connection:
                store._lock(connection, "scheduler")
                store._lock(connection, "accounts")
                if store.dialect == "postgresql":
                    connection.exec_driver_sql("LOCK TABLE records,sessions,audit_events,job_leases,usernames IN EXCLUSIVE MODE")
                if any(connection.execute(select(func.count()).select_from(table)).scalar_one() for table in (records, sessions, audit_events, job_leases, usernames)):
                    raise ValueError("Target changed; import was refused")
                interrupted_tickets = set()
                for row in db.execute("SELECT * FROM records ORDER BY created_at,id"):
                    value = json.loads(row["payload"])
                    if not isinstance(value, dict) or value.get("id") != row["id"] or not re.fullmatch(r"[a-f0-9]{32}", row["id"]):
                        raise ValueError("Invalid legacy record identity")
                    if row["kind"] == "task" and value.get("state") in ("queued", "running"):
                        value.update(state="interrupted", completed_at=utc_now(), error="Legacy local execution was not replayed during migration")
                        interrupted_tickets.add(value.get("ticket_id"))
                    connection.execute(insert(records).values(id=row["id"], kind=row["kind"], environment_id=row["environment_id"],
                        parent_id=row["parent_id"], created_at=row["created_at"], payload=value, version=1))
                    if row["kind"] == "user":
                        name = value.get("username", "")
                        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", name):
                            raise ValueError("Invalid legacy account name")
                        connection.execute(insert(usernames).values(user_id=row["id"], username=name.casefold()))
                for ticket_id in interrupted_tickets:
                    ticket = store._record(connection, "ticket", ticket_id, lock=True)
                    if ticket:
                        value = dict(ticket["payload"])
                        value["status"] = "needs_information"
                        store._save(connection, ticket, value)
                for row in db.execute("SELECT * FROM audit ORDER BY id"):
                    connection.execute(insert(audit_events).values(id=uuid.uuid4().hex, at=row["at"], actor=row["actor"],
                        action=row["action"], resource_kind="legacy_record", resource_id=row["target"], outcome="legacy_import", request_id=None, detail={}))
            return {**summary, "imported_records": counts["records"], "imported_audit": counts["audit"]}
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit workbench schema migration; never prints credentials or records")
    parser.add_argument("action", choices=("initialize", "copy-legacy"))
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--apply", action="store_true", help="Without this flag copy-legacy is a read-only dry run")
    args = parser.parse_args()
    try:
        if args.database_url_file.stat().st_size > 4096 or any(path.is_symlink() or path.is_junction() for path in (args.database_url_file.absolute(), *args.database_url_file.absolute().parents)):
            raise ValueError("Database URL file is invalid")
        url = args.database_url_file.read_text(encoding="utf-8").strip()
        if args.action == "initialize":
            initialize_database(url)
            result = {"initialized": True, "schema_revision": SCHEMA_REVISION}
        else:
            if args.source is None:
                raise ValueError("An explicit legacy source is required")
            result = copy_legacy_sqlite(args.source, url, dry_run=not args.apply)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"error": "migration_refused", "source_preserved": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
