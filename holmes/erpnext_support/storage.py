"""Transactional workbench store; PostgreSQL and isolated SQLite only."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import create_engine, delete, event, func, inspect, insert, or_, select, update
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError

from holmes.erpnext_support.persistence.schema import (
    SCHEMA_REVISION, audit_events, job_leases, metadata, records, schema_info,
    sessions, store_locks, usernames,
)
from holmes.erpnext_support.security import sanitize
from holmes.erpnext_support.observability import request_id_context


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationRequired(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    pass


class TaskConflict(RuntimeError):
    pass


class UserUnique(RuntimeError):
    pass


class AdminInvariant(RuntimeError):
    pass


def database_engine(location: Path | str):
    """Only explicit workbench targets; suppress SQL values in error messages."""
    if isinstance(location, Path) or "://" not in str(location):
        path = Path(location).absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = make_url("sqlite+pysqlite://").set(database=str(path))
    else:
        url = make_url(str(location))
        path = Path(url.database).absolute() if url.drivername.startswith("sqlite") and url.database not in (None, ":memory:") else None
    if url.drivername not in ("sqlite", "sqlite+pysqlite", "postgresql+pg8000"):
        raise ValueError("Only SQLite and postgresql+pg8000 workbench databases are supported")
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, echo=False,
                           connect_args={"timeout": 15, "check_same_thread": False} if url.drivername.startswith("sqlite") else {"timeout": 15})
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(db, _record):
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
    return engine, path


class Store:
    def __init__(self, location: Path | str, *, initialize: bool | None = None):
        self.engine, self.path = database_engine(location)
        self.dialect = self.engine.dialect.name
        existing = inspect(self.engine).get_table_names()
        if "records" in existing and "version" not in {column["name"] for column in inspect(self.engine).get_columns("records")}:
            self.engine.dispose()
            raise MigrationRequired("Legacy SQLite is preserved; copy-import it into a new database")
        if initialize is None:
            initialize = self.dialect == "sqlite"
        if not existing and initialize:
            with self.engine.begin() as db:
                metadata.create_all(db)
                db.execute(insert(schema_info).values(revision=SCHEMA_REVISION))
                db.execute(insert(store_locks), [{"name": "scheduler"}, {"name": "accounts"}])
                if self.dialect == "sqlite":
                    db.exec_driver_sql("CREATE VIEW audit AS SELECT id,at,actor,action,resource_id AS target FROM audit_events")
        if not self.readiness()["ready"]:
            self.engine.dispose()
            raise MigrationRequired("Workbench schema is missing or incompatible; run explicit migrations")

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Connection]:
        with self.engine.connect() as db:
            if self.dialect == "sqlite":
                db.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            else:
                db.begin()
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Legacy local backup/check only; API uses explicit session methods."""
        if self.dialect != "sqlite" or self.path is None:
            raise RuntimeError("Raw SQLite compatibility is unavailable for PostgreSQL")
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def readiness(self) -> dict:
        try:
            with self.engine.connect() as db:
                if not set(metadata.tables).issubset(set(inspect(db).get_table_names())):
                    return {"ready": False, "database": self.dialect, "schema_revision": None}
                ready = db.execute(select(schema_info.c.revision)).scalar_one_or_none() == SCHEMA_REVISION
                ready = ready and set(db.execute(select(store_locks.c.name)).scalars()) == {"scheduler", "accounts"}
                db.execute(select(records.c.version).limit(1))
            return {"ready": bool(ready), "database": self.dialect, "schema_revision": SCHEMA_REVISION if ready else None}
        except Exception:
            return {"ready": False, "database": self.dialect, "schema_revision": None}

    def _lock(self, db, name):
        db.execute(select(store_locks.c.name).where(store_locks.c.name == name).with_for_update()).scalar_one()

    def _record(self, db, kind, identifier, *, lock=False):
        statement = select(records).where(records.c.kind == kind, records.c.id == identifier)
        return db.execute(statement.with_for_update() if lock else statement).mappings().first()

    def _insert(self, db, kind, values, environment_id=None, parent_id=None):
        obj = deepcopy({**values, "id": uuid.uuid4().hex, "created_at": utc_now()})
        db.execute(insert(records).values(id=obj["id"], kind=kind, environment_id=environment_id, parent_id=parent_id,
                                        created_at=obj["created_at"], payload=obj, version=1))
        if kind == "task" and obj.get("state") in ("queued", "running"):
            db.execute(insert(job_leases).values(task_id=obj["id"], ticket_id=obj.get("ticket_id", parent_id or ""), state=obj["state"], created_at=obj["created_at"]))
        return obj

    def _save(self, db, row, obj):
        if obj.get("id") != row["id"]:
            raise ValueError("Record identity cannot change")
        obj["updated_at"] = utc_now()
        result = db.execute(update(records).where(records.c.id == row["id"], records.c.version == row["version"])
                            .values(payload=obj, created_at=obj["created_at"], version=row["version"] + 1))
        if result.rowcount != 1:
            raise VersionConflict("Record changed concurrently")
        if row["kind"] == "task" and row["payload"].get("state") != obj.get("state"):
            db.execute(update(job_leases).where(job_leases.c.task_id == row["id"]).values(state=obj.get("state", "unknown")))
        return obj

    def add(self, kind, values, environment_id=None, parent_id=None):
        if kind == "user":
            return self.create_user_unique(values)
        with self.transaction(write=True) as db:
            if kind == "ticket" and environment_id is not None:
                environment = self._record(db, "environment", environment_id, lock=True)
                if environment is None and re.fullmatch(r"[a-f0-9]{32}", environment_id):
                    raise KeyError(environment_id)
            return self._insert(db, kind, values, environment_id, parent_id)

    def get(self, kind, identifier):
        with self.transaction() as db:
            row = self._record(db, kind, identifier)
            return deepcopy(row["payload"]) if row else None

    def list(self, kind, parent_id=None, limit=200, offset=0):
        statement = select(records.c.payload).where(records.c.kind == kind)
        if parent_id is not None:
            statement = statement.where(records.c.parent_id == parent_id)
        with self.transaction() as db:
            return [deepcopy(row) for row in db.execute(statement.order_by(records.c.created_at.desc(), records.c.id.desc())
                                                       .limit(max(0, min(limit, 1000))).offset(max(0, offset))).scalars()]

    def page(self, kind, limit=50, offset=0, filters=None, environment_ids=None):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Invalid pagination")
        conditions = [records.c.kind == kind]
        if environment_ids is not None:
            conditions.append((records.c.id if kind == "environment" else records.c.environment_id).in_(environment_ids))
        for key, value in (filters or {}).items():
            if key == "q":
                fields = {"ticket": ("delivery_note", "description"), "case": ("title", "symptom"), "environment": ("name",), "user": ("username",)}.get(kind, ())
                if fields:
                    conditions.append(or_(*(records.c.payload[field].as_string().contains(str(value)[:140], autoescape=True) for field in fields)))
            elif key == "environment_id":
                conditions.append(records.c.environment_id == value)
            elif key in ("status", "role", "disabled", "owner", "erpnext_version"):
                column = func.coalesce(records.c.payload[key].as_boolean(), False) if key == "disabled" and isinstance(value, bool) else records.c.payload[key].as_boolean() if isinstance(value, bool) else records.c.payload[key].as_string()
                conditions.append(column == value)
            else:
                raise ValueError("Unsupported record filter")
        with self.transaction() as db:
            # The same snapshot is required for count and page on PostgreSQL.
            if self.dialect == "postgresql":
                db.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            total = db.execute(select(func.count()).select_from(records).where(*conditions)).scalar_one()
            items = [deepcopy(row) for row in db.execute(select(records.c.payload).where(*conditions)
                      .order_by(records.c.created_at.desc(), records.c.id.desc()).limit(limit).offset(offset)).scalars()]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_by_username(self, username):
        with self.transaction() as db:
            return deepcopy(db.execute(select(records.c.payload).join(usernames, usernames.c.user_id == records.c.id)
                                       .where(usernames.c.username == str(username).casefold(), records.c.kind == "user")).scalar_one_or_none())

    find_user = get_by_username

    def update(self, kind, identifier, mutate: Callable[[dict], None], *, expected_version=None):
        with self.transaction(write=True) as db:
            if kind == "task":
                db.execute(select(job_leases).where(job_leases.c.task_id == identifier).with_for_update()).first()
            row = self._record(db, kind, identifier, lock=True)
            if row is None:
                raise KeyError(identifier)
            if expected_version is not None and row["version"] != expected_version:
                raise VersionConflict("Record version mismatch")
            obj = deepcopy(row["payload"])
            mutate(obj)
            if kind == "user":
                if obj.get("username") != row["payload"].get("username"):
                    raise ValueError("Username cannot be changed through generic update")
                if any(obj.get(key) != row["payload"].get(key) for key in ("role", "disabled", "environment_ids", "password_hash")):
                    db.execute(delete(sessions).where(sessions.c.user_id == identifier))
            return self._save(db, row, obj)

    def record_version(self, kind, identifier):
        with self.transaction() as db:
            row = self._record(db, kind, identifier)
            return row["version"] if row else None

    def delete(self, kind, identifier):
        with self.transaction(write=True) as db:
            db.execute(delete(records).where(records.c.kind == kind, records.c.id == identifier))

    def delete_environment_guarded(self, identifier):
        with self.transaction(write=True) as db:
            if self._record(db, "environment", identifier, lock=True) is None:
                raise KeyError(identifier)
            count = db.execute(select(func.count()).select_from(records).where(records.c.kind == "ticket", records.c.environment_id == identifier)).scalar_one()
            if count:
                raise TaskConflict("已有工单引用此环境，不能删除")
            db.execute(delete(records).where(records.c.kind == "environment", records.c.id == identifier))

    def add_action(self, ticket_id, values):
        with self.transaction(write=True) as db:
            self._lock(db, "scheduler")
            ticket = self._record(db, "ticket", ticket_id, lock=True)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket["payload"].get("status") in ("closed", "investigating"):
                raise TaskConflict("已关闭或调查中的工单不能记录人工处理")
            obj = self._insert(db, "action", values, ticket["environment_id"], ticket_id)
            updated = deepcopy(ticket["payload"])
            updated["status"] = "awaiting_review"
            self._save(db, ticket, updated)
            return obj

    def close_ticket(self, ticket_id, review_id, confirmation, actor, manual_override=False):
        with self.transaction(write=True) as db:
            self._lock(db, "scheduler")
            ticket = self._record(db, "ticket", ticket_id, lock=True)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket["payload"].get("status") != "awaiting_review":
                raise TaskConflict("工单必须处于待复核阶段且没有执行中的任务")
            latest = db.execute(select(records.c.payload).where(records.c.parent_id == ticket_id, records.c.kind == "task")
                                .order_by(records.c.created_at.desc(), records.c.id.desc()).limit(1)).scalar_one_or_none()
            action = db.execute(select(records.c.payload).where(records.c.parent_id == ticket_id, records.c.kind == "action")
                                .order_by(records.c.created_at.desc(), records.c.id.desc()).limit(1)).scalar_one_or_none()
            if not latest or latest.get("id") != review_id or latest.get("kind") != "review" or latest.get("ticket_id") != ticket_id or latest.get("state") not in ("completed", "partial"):
                raise TaskConflict("必须选择本工单最新且已完成的复核任务")
            if not action or action["created_at"] > latest["created_at"]:
                raise TaskConflict("人工处理记录需要先于该复核，新增处理后须重新复核")
            verified = (latest.get("result") or {}).get("review", {}).get("business_complete") is True
            if not verified and (not manual_override or len(str(confirmation).strip()) < 20):
                raise TaskConflict("业务完成证据不足，需要至少20字的人工例外说明")
            obj = deepcopy(ticket["payload"])
            obj.update(status="closed", closed_by=actor, closed_at=utc_now(), close_confirmation=sanitize(confirmation), review_id=review_id, manual_override=manual_override)
            return self._save(db, ticket, obj)

    def ticket_snapshot(self, identifier):
        # A single SELECT retains one atomic view under READ COMMITTED.
        children = []
        for kind in ("task", "action"):
            identifiers = select(records.c.id).where(records.c.parent_id == identifier, records.c.kind == kind)
            identifiers = identifiers.order_by(records.c.created_at.desc(), records.c.id.desc()).limit(200).correlate(None)
            children.append(records.c.id.in_(identifiers))
        with self.transaction() as db:
            rows = db.execute(select(records).where(((records.c.id == identifier) & (records.c.kind == "ticket")) | or_(*children))
                .order_by(records.c.created_at.desc(), records.c.id.desc())).mappings().all()
        ticket = next((deepcopy(row["payload"]) for row in rows if row["kind"] == "ticket" and row["id"] == identifier), None)
        if ticket is None:
            return None
        tasks = [deepcopy(row["payload"]) for row in rows if row["kind"] == "task"][:200]
        actions = [deepcopy(row["payload"]) for row in rows if row["kind"] == "action"][:200]
        return {**ticket, "tasks": tasks, "actions": actions, "reviews": [task for task in tasks if task.get("kind") == "review"],
                "latest_result": next((task.get("result") for task in tasks if task.get("result")), None)}

    def create_session(self, token_hash, user_id, csrf, expires_at):
        if not re.fullmatch(r"[a-f0-9]{64}", token_hash) or not 1 <= len(csrf) <= 128:
            raise ValueError("Invalid session metadata")
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError("Session expiry must be future and timezone aware")
        obj = {"token_hash": token_hash, "user_id": user_id, "csrf": csrf, "expires_at": expiry.astimezone(timezone.utc).isoformat()}
        with self.transaction(write=True) as db:
            user = self._record(db, "user", user_id, lock=True)
            if user is None or user["payload"].get("disabled"):
                raise PermissionError("Account is disabled or missing")
            db.execute(delete(sessions).where(sessions.c.expires_at <= utc_now()))
            db.execute(insert(sessions).values(**obj))
        return obj

    def get_session(self, token_hash):
        with self.transaction() as db:
            row = db.execute(select(sessions, records.c.payload).join(records, sessions.c.user_id == records.c.id)
                             .where(sessions.c.token_hash == token_hash, sessions.c.expires_at > utc_now(), records.c.kind == "user")).mappings().first()
            return None if row is None or row["payload"].get("disabled") else {key: row[key] for key in ("token_hash", "user_id", "csrf", "expires_at")}

    def delete_session(self, token_hash):
        with self.transaction(write=True) as db:
            db.execute(delete(sessions).where(sessions.c.token_hash == token_hash))

    def revoke_user_sessions(self, user_id):
        with self.transaction(write=True) as db:
            self._record(db, "user", user_id, lock=True)
            return db.execute(delete(sessions).where(sessions.c.user_id == user_id)).rowcount

    def create_user_unique(self, values):
        username = values.get("username", "")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise ValueError("Invalid account name")
        try:
            with self.transaction(write=True) as db:
                self._lock(db, "accounts")
                obj = self._insert(db, "user", values)
                db.execute(insert(usernames).values(user_id=obj["id"], username=username.casefold()))
                return obj
        except IntegrityError:
            raise UserUnique("Account name already exists") from None

    def update_user_admin_guard(self, identifier, values):
        if set(values) - {"role", "environment_ids", "disabled", "password_hash"}:
            raise ValueError("Unsupported account mutation")
        with self.transaction(write=True) as db:
            self._lock(db, "accounts")
            row = self._record(db, "user", identifier, lock=True)
            if row is None:
                raise KeyError(identifier)
            obj = deepcopy(row["payload"])
            obj.update(values)
            if row["payload"].get("role") == "admin" and not row["payload"].get("disabled") and (obj.get("role") != "admin" or obj.get("disabled")):
                others = db.execute(select(records.c.payload).where(records.c.kind == "user", records.c.id != identifier)).scalars()
                if not any(user.get("role") == "admin" and not user.get("disabled") for user in others):
                    raise AdminInvariant("The last enabled administrator cannot be removed")
            result = self._save(db, row, obj)
            db.execute(delete(sessions).where(sessions.c.user_id == identifier))
            return result

    def audit(self, actor, action, target=None, resource_id=None, outcome="success", request_id=None, detail=None, *, resource_kind=None):
        if resource_kind is None:
            resource_kind = str(target) if resource_id is not None else "record"
        resource_id = resource_id if resource_id is not None else str(target or "")
        request_id = request_id if request_id is not None else request_id_context.get()
        detail = sanitize(detail or {})
        if len(json.dumps(detail, ensure_ascii=False).encode("utf-8")) > 4096:
            detail = {"truncated": True}
        obj = {"id": uuid.uuid4().hex, "at": utc_now(), "actor": sanitize(str(actor))[:140], "action": sanitize(str(action))[:140],
               "resource_kind": sanitize(resource_kind)[:64], "resource_id": sanitize(resource_id)[:140], "outcome": sanitize(outcome)[:32],
               "request_id": sanitize(request_id)[:140] if request_id else None, "detail": detail}
        with self.transaction(write=True) as db:
            db.execute(insert(audit_events).values(**obj))

    def list_audit(self, limit=100, offset=0, filters=None):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Invalid audit pagination")
        conditions = []
        for key, value in (filters or {}).items():
            if key == "q":
                conditions.append(or_(*(audit_events.c[field].contains(str(value)[:140], autoescape=True) for field in ("request_id", "actor", "action"))))
            elif key in ("actor", "action", "outcome", "resource_kind"):
                conditions.append(audit_events.c[key] == value)
            else:
                raise ValueError("Unsupported audit filter")
        with self.transaction() as db:
            if self.dialect == "postgresql":
                db.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            total = db.execute(select(func.count()).select_from(audit_events).where(*conditions)).scalar_one()
            items = [{**dict(row), "created_at": row["at"]} for row in db.execute(select(audit_events).where(*conditions)
                     .order_by(audit_events.c.at.desc(), audit_events.c.id.desc()).limit(limit).offset(offset)).mappings()]
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def create_task(self, ticket_id, values, active_limit=4):
        if not 1 <= active_limit <= 100:
            raise ValueError("Invalid active task limit")
        with self.transaction(write=True) as db:
            self._lock(db, "scheduler")
            ticket = self._record(db, "ticket", ticket_id, lock=True)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket["payload"].get("status") == "closed":
                raise TaskConflict("Closed tickets cannot enqueue diagnosis")
            active = db.execute(select(job_leases.c.ticket_id).where(job_leases.c.state.in_(("queued", "running")))).scalars().all()
            if ticket_id in active or len(active) >= active_limit:
                raise TaskConflict("Ticket has an active task or task capacity is reached")
            obj = self._insert(db, "task", {**values, "ticket_id": ticket_id, "environment_id": ticket["environment_id"],
                                           "ticket_snapshot": deepcopy(ticket["payload"]), "state": "queued"}, ticket["environment_id"], ticket_id)
            updated = deepcopy(ticket["payload"])
            updated["status"] = "investigating"
            self._save(db, ticket, updated)
            return obj

    @staticmethod
    def _validate_lease(worker_id, lease_seconds):
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 140 or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid worker lease metadata")

    def _now(self, db):
        # All PostgreSQL nodes share the database clock for ownership fencing.
        return float(db.execute(select(func.extract("epoch", func.clock_timestamp()))).scalar_one()) if self.dialect == "postgresql" else time.time()

    def _owned_job(self, db, task_id, worker_id):
        job = db.execute(select(job_leases).where(job_leases.c.task_id == task_id).with_for_update()).mappings().first()
        return job if job and job["state"] == "running" and job["worker_id"] == worker_id and (job["lease_until"] or 0) > self._now(db) else None

    def claim_task(self, task_id, worker_id, lease_seconds=60):
        self._validate_lease(worker_id, lease_seconds)
        with self.transaction(write=True) as db:
            self._lock(db, "scheduler")
            job = db.execute(select(job_leases).where(job_leases.c.task_id == task_id).with_for_update()).mappings().first()
            if job is None or job["state"] != "queued":
                return None
            row = self._record(db, "task", task_id, lock=True)
            if row is None or row["payload"].get("state") != "queued":
                return None
            obj = deepcopy(row["payload"])
            obj.update(state="running", worker_id=worker_id, lease_until=self._now(db) + lease_seconds, started_at=utc_now())
            db.execute(update(job_leases).where(job_leases.c.task_id == task_id).values(worker_id=worker_id, lease_until=obj["lease_until"], state="running"))
            return self._save(db, row, obj)

    def heartbeat_task(self, task_id, worker_id, lease_seconds=60):
        self._validate_lease(worker_id, lease_seconds)
        with self.transaction(write=True) as db:
            if self._owned_job(db, task_id, worker_id) is None:
                return False
            row = self._record(db, "task", task_id, lock=True)
            obj = deepcopy(row["payload"])
            obj["lease_until"] = self._now(db) + lease_seconds
            db.execute(update(job_leases).where(job_leases.c.task_id == task_id).values(lease_until=obj["lease_until"]))
            self._save(db, row, obj)
            return True

    def step_task(self, task_id, worker_id, step):
        with self.transaction(write=True) as db:
            if self._owned_job(db, task_id, worker_id) is None:
                return False
            row = self._record(db, "task", task_id, lock=True)
            obj = deepcopy(row["payload"])
            obj.setdefault("steps", []).append(deepcopy(step))
            self._save(db, row, obj)
            return True

    def finish_task(self, task_id, ticket_id, values, ticket_status, *, worker_id=None):
        with self.transaction(write=True) as db:
            self._lock(db, "scheduler")
            ticket = self._record(db, "ticket", ticket_id, lock=True)
            if ticket is None:
                raise KeyError(ticket_id)
            job = db.execute(select(job_leases).where(job_leases.c.task_id == task_id).with_for_update()).mappings().first()
            if worker_id is not None:
                if self._owned_job(db, task_id, worker_id) is None or job["ticket_id"] != ticket_id:
                    return False
            elif job and job["worker_id"]:
                return False
            row = self._record(db, "task", task_id, lock=True)
            if row is None:
                raise KeyError(task_id)
            if row["payload"].get("ticket_id") != ticket_id:
                raise TaskConflict("Task is bound to another ticket")
            if row["payload"].get("state") not in ("queued", "running"):
                return False
            if values.get("state") not in ("completed", "partial", "failed", "interrupted"):
                raise ValueError("Completion requires a terminal state")
            obj = deepcopy(row["payload"])
            obj.update(deepcopy(values))
            self._save(db, row, obj)
            updated = deepcopy(ticket["payload"])
            updated["status"] = ticket_status
            self._save(db, ticket, updated)
            return True

    def queued_tasks(self, limit=100):
        with self.transaction() as db:
            return [deepcopy(row) for row in db.execute(select(records.c.payload).join(job_leases, job_leases.c.task_id == records.c.id)
                    .where(job_leases.c.state == "queued").order_by(job_leases.c.created_at).limit(max(0, min(limit, 200)))).scalars()]

    def dispatched_task(self, task_id, publish_error=None):
        with self.transaction(write=True) as db:
            job = db.execute(select(job_leases).where(job_leases.c.task_id == task_id).with_for_update()).mappings().first()
            if job is None or job["state"] != "queued":
                return False
            row = self._record(db, "task", task_id, lock=True)
            obj = deepcopy(row["payload"])
            obj.update(last_dispatched_at=utc_now(), publish_error="queue_publish_failed" if publish_error else None)
            db.execute(update(job_leases).where(job_leases.c.task_id == task_id).values(last_dispatched_at=time.time()))
            self._save(db, row, obj)
            return True

    def reconcile_expired(self):
        count = 0
        with self.transaction(write=True) as db:
            self._lock(db, "scheduler")
            jobs = db.execute(select(job_leases).where(job_leases.c.state == "running", job_leases.c.lease_until <= self._now(db))
                              .order_by(job_leases.c.ticket_id).with_for_update()).mappings().all()
            for job in jobs:
                row = self._record(db, "task", job["task_id"], lock=True)
                obj = deepcopy(row["payload"])
                obj.update(state="interrupted", completed_at=utc_now(), error="Worker lease expired; saved evidence is preserved")
                self._save(db, row, obj)
                ticket = self._record(db, "ticket", job["ticket_id"], lock=True)
                if ticket:
                    updated = deepcopy(ticket["payload"])
                    updated["status"] = "needs_information"
                    self._save(db, ticket, updated)
                count += 1
        return count

    def recover_interrupted(self):
        """Local compatibility only; enterprise API startup does not stop peers."""
        if self.dialect != "sqlite":
            return self.reconcile_expired()
        count = 0
        for task in self.list("task", limit=1000):
            if task.get("state") in ("queued", "running") and not task.get("worker_id"):
                self.update("task", task["id"], lambda obj: obj.update(state="interrupted", completed_at=utc_now(), error="服务重启中断了诊断；已保存步骤，可重新执行。"))
                if self.get("ticket", task.get("ticket_id", "")):
                    self.update("ticket", task["ticket_id"], lambda obj: obj.update(status="needs_information"))
                count += 1
        return count + self.reconcile_expired()

    def expire_evidence(self, days):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        count = 0
        with self.transaction(write=True) as db:
            for row in db.execute(select(records).where(records.c.kind == "task", records.c.created_at < cutoff).with_for_update()).mappings():
                obj = deepcopy(row["payload"])
                if obj.get("state") not in ("queued", "running") and (obj.get("result") or obj.get("steps")):
                    obj.update(result=None, steps=[], evidence_expired=True)
                    self._save(db, row, obj)
                    count += 1
        return count

    def backup(self, destination):
        if self.dialect != "sqlite":
            raise RuntimeError("PostgreSQL backup requires explicit administrator pg_dump")
        # Exclusive reservation also prevents two concurrent backup requests
        # from both passing an exists check and replacing the same file.
        with destination.open("xb"):
            pass
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)

    def close(self):
        self.engine.dispose()
