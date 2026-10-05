"""Workbench-only SQLite storage, independent of the ERP database."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    environment_id TEXT, parent_id TEXT,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS records_kind ON records(kind, created_at);
                CREATE INDEX IF NOT EXISTS records_parent ON records(parent_id, kind);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, kind: str, values: dict, environment_id: str | None = None,
            parent_id: str | None = None) -> dict:
        obj = {**values, "id": uuid.uuid4().hex, "created_at": utc_now()}
        with self.connect() as db:
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?)",
                       (obj["id"], kind, environment_id, parent_id, obj["created_at"], json.dumps(obj, ensure_ascii=False)))
        return obj

    def get(self, kind: str, identifier: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str, parent_id: str | None = None, limit: int = 200) -> list[dict]:
        with self.connect() as db:
            if parent_id is not None:
                rows = db.execute("SELECT payload FROM records WHERE kind=? AND parent_id=? ORDER BY created_at DESC LIMIT ?",
                                  (kind, parent_id, min(limit, 200))).fetchall()
            else:
                rows = db.execute("SELECT payload FROM records WHERE kind=? ORDER BY created_at DESC LIMIT ?",
                                  (kind, min(limit, 200))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def ticket_snapshot(self, identifier: str) -> dict | None:
        """Read ticket and its tasks from the same WAL snapshot.

        Completion may occur during a request. Independent reads can otherwise
        show a completed task with the previous investigating ticket state.
        """
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT payload FROM records WHERE kind='ticket' AND id=?", (identifier,)).fetchone()
            if not row:
                return None
            ticket = json.loads(row[0])
            children = db.execute("SELECT kind,payload FROM records WHERE parent_id=? AND kind IN ('task','action') ORDER BY created_at DESC LIMIT 400", (identifier,)).fetchall()
            tasks = [json.loads(r["payload"]) for r in children if r["kind"] == "task"]
            actions = [json.loads(r["payload"]) for r in children if r["kind"] == "action"]
        return {**ticket, "tasks": tasks, "actions": actions,
                "reviews": [t for t in tasks if t["kind"] == "review"],
                "latest_result": next((t.get("result") for t in tasks if t.get("result")), None)}

    def update(self, kind: str, identifier: str, mutate: Callable[[dict], None]) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            if not row:
                raise KeyError(identifier)
            obj = json.loads(row[0])
            mutate(obj)
            obj["updated_at"] = utc_now()
            db.execute("UPDATE records SET payload=?,created_at=? WHERE id=?", (json.dumps(obj, ensure_ascii=False), obj["created_at"], identifier))
        return obj

    def delete(self, kind: str, identifier: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, identifier))

    def finish_task(self, task_id: str, ticket_id: str, values: dict, ticket_status: str) -> None:
        """Publish task completion and its ticket transition in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for kind, identifier, changes in (("task", task_id, values), ("ticket", ticket_id, {"status": ticket_status})):
                row = db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
                if not row:
                    raise KeyError(identifier)
                obj = json.loads(row[0])
                obj.update(changes, updated_at=utc_now())
                db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(obj, ensure_ascii=False), identifier))

    def audit(self, actor: str, action: str, target: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)", (utc_now(), actor, action, target))

    def recover_interrupted(self) -> int:
        count = 0
        with self.connect() as db:
            identifiers = [r[0] for r in db.execute("SELECT id FROM records WHERE kind='task' AND json_extract(payload,'$.state') IN ('queued','running')")]
        for identifier in identifiers:
            task = self.get("task", identifier)
            if task and task["state"] in ("queued", "running"):
                self.update("task", task["id"], lambda t: t.update(state="interrupted", completed_at=utc_now(),
                            error="服务重启中断了诊断；已保存步骤，可重新执行。"))
                ticket_id = task["ticket_id"]
                if self.get("ticket", ticket_id):
                    self.update("ticket", ticket_id, lambda t: t.update(status="needs_information"))
                count += 1
        return count

    def expire_evidence(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        count = 0
        with self.connect() as db:
            identifiers = [r[0] for r in db.execute("SELECT id FROM records WHERE kind='task' AND created_at<?", (cutoff,))]
        for identifier in identifiers:
            task = self.get("task", identifier)
            if task and task["created_at"] < cutoff and (task.get("result") or task.get("steps")):
                self.update("task", task["id"], lambda t: t.update(result=None, steps=[], evidence_expired=True))
                count += 1
        return count

    def backup(self, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError("拒绝覆盖现有备份")
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
