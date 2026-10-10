"""Shared diagnosis runner: database leases fence duplicate or late workers."""

import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from celery import Celery
from kombu import Queue
from redis import Redis

from holmes.erpnext_support.engine import diagnose
from holmes.erpnext_support.observability import correlation, event
from holmes.erpnext_support.settings import Settings
from holmes.erpnext_support.storage import Store, utc_now


TASK_NAME = "erpnext_support.diagnose"


def can_access(store: Store, user: dict, environment: dict) -> bool:
    current = store.get("user", user["id"])
    live = store.get("environment", environment["id"])
    fields = ("base_url", "credential_ref", "collector_ref", "allowed_services", "erpnext_version", "frappe_version", "model_consent")
    return bool(current and live and not current.get("disabled") and
                (current["role"] == "admin" or environment["id"] in current["environment_ids"]) and
                all(live.get(k) == environment.get(k) for k in fields))


def client(settings: Settings) -> Celery:
    app = Celery("erpnext_support", broker=settings.broker_url)
    app.conf.update(
        task_serializer="json", accept_content=["json"], result_serializer="json",
        task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
        task_default_queue="erp_support", worker_prefetch_multiplier=1,
        task_queues=(Queue("erp_support"),), task_create_missing_queues=False, worker_enable_remote_control=False,
        broker_connection_retry_on_startup=True, broker_connection_timeout=3,
        broker_transport_options={"visibility_timeout": 300, "socket_timeout": 3, "socket_connect_timeout": 3},
        task_publish_retry=False, task_soft_time_limit=210, task_time_limit=240,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        worker_hijack_root_logger=False, worker_redirect_stdouts=False,
    )
    return app


def broker_ready(settings: Settings) -> bool:
    if settings.queue_backend != "celery":
        return True
    try:
        with Redis.from_url(settings.broker_url, socket_connect_timeout=2, socket_timeout=2) as connection:
            return connection.ping() is True
    except Exception:
        return False


def allow_login(settings: Settings, identity: str) -> bool:
    """Shared fixed-window limiter; identifier is a hash, never an IP or name."""
    with Redis.from_url(settings.broker_url, socket_connect_timeout=2, socket_timeout=2) as connection:
        key = "erp_support:login:" + identity
        count = connection.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", 1, key)
        return count <= 15


def worker_ready(store: Store) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    return any(w.get("heartbeat_at", "") > cutoff for w in store.list("worker", limit=100))


def dispatch(settings: Settings, store: Store, task_id: str) -> bool:
    try:
        client(settings).send_task(TASK_NAME, args=[task_id], task_id=task_id)
        store.dispatched_task(task_id)
        return True
    except Exception:
        store.dispatched_task(task_id, publish_error="broker_unavailable")
        event("task_publish_failed", task_id=task_id, error_type="broker_unavailable")
        return False


def dispatch_pending(settings: Settings, store: Store) -> int:
    store.reconcile_expired()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    sent = 0
    for task in store.queued_tasks(limit=settings.task_capacity):
        if task.get("last_dispatched_at", "") < cutoff:
            sent += int(dispatch(settings, store, task["id"]))
    return sent


def run_task(settings: Settings, store: Store, task_id: str, diagnostic: Callable = diagnose, worker_id: str | None = None) -> bool:
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
        return False
    owner = worker_id or uuid.uuid4().hex
    task = store.claim_task(task_id, owner, settings.task_lease_seconds)
    if not task:
        return False
    start = time.perf_counter()
    stop, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not stop.wait(10):
            try:
                if not store.heartbeat_task(task_id, owner, settings.task_lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    with correlation(task.get("request_id", task_id)):
        try:
            user = store.get("user", task["request_user_id"])
            environment = task["environment_snapshot"]
            ticket = task["ticket_snapshot"]
            if not user or not can_access(store, user, environment):
                raise PermissionError("environment_authorization_changed")
            creds = settings.credentials(environment["credential_ref"])

            def authorized() -> bool:
                return not lost.is_set() and can_access(store, user, environment)

            def step(value: dict):
                if not authorized() or not store.step_task(task_id, owner, value):
                    lost.set()
                    raise PermissionError("task_lease_or_authorization_lost")

            cases = [c for c in store.list("case", limit=1000) if c["status"] == "approved"]
            result = diagnostic(settings, environment, ticket, creds, authorized, step, cases)
            if not authorized():
                raise PermissionError("task_lease_or_authorization_lost")
            if task["kind"] == "review":
                state = result.get("business_state", {})
                blocking = [c for c in result.get("causes", []) if c.get("code") != "known_issue_reference"]
                complete = state.get("business_complete") is True and result.get("evidence_consistent", True) and not blocking
                previous = next((t for t in store.list("task", ticket["id"]) if t["kind"] == "diagnosis" and t.get("result")), None)
                result["review"] = {
                    "business_complete": complete, "requires_human_confirmation": True,
                    "previous_task_id": previous["id"] if previous else None,
                    "subtype_checks": [{"code": c["code"], "present_after_handling": any(x["code"] == c["code"] for x in blocking)}
                                       for c in previous.get("result", {}).get("causes", [])] if previous else [],
                    "limit": "业务完成须读取已提交发货单及实际出库流水；接口可用或状态变化不证明原候选原因。",
                }
            partial = bool(result.get("missing")) or result.get("metrics", {}).get("tool_success_rate", 0) < 1
            next_status = "awaiting_review" if task["kind"] == "review" else "needs_information" if partial else "awaiting_action"
            state = "partial" if partial else "completed"
            saved = store.finish_task(task_id, ticket["id"], {
                "state": state, "result": result, "completed_at": utc_now(), "elapsed_seconds": time.perf_counter() - start,
            }, next_status, worker_id=owner)
            if saved:
                store.audit(user["username"], "task_completed", task_id, resource_kind="task", detail={"state": state})
                event("task_completed", task_id=task_id, state=state, elapsed_seconds=time.perf_counter() - start)
            return bool(saved)
        except Exception as exc:
            saved = store.finish_task(task_id, task["ticket_id"], {
                "state": "failed", "completed_at": utc_now(), "elapsed_seconds": time.perf_counter() - start,
                "error": f"任务未完成 ({type(exc).__name__})；已完成步骤保留，可重新执行。",
            }, "needs_information", worker_id=owner)
            if saved:
                store.audit(task.get("requested_by", "worker"), "task_failed", task_id, resource_kind="task", outcome="error", detail={"error_type": type(exc).__name__})
            event("task_failed", task_id=task_id, error_type=type(exc).__name__)
            return False
        finally:
            stop.set()
            thread.join(timeout=1)
