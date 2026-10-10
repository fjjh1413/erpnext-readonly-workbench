"""Bounded metrics and structured logs without business data or credentials."""

import contextvars
import json
import logging
import re
import uuid
from contextlib import contextmanager

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select

from holmes.erpnext_support.persistence.schema import records


request_id_context = contextvars.ContextVar("erp_support_request_id", default="")
LOGGER = logging.getLogger("erp_support.events")


def configure_logging() -> None:
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def request_id(value: str | None = None) -> str:
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value) else uuid.uuid4().hex


def event(name: str, **values) -> None:
    allowed = {k: v for k, v in values.items() if k in (
        "request_id", "method", "route", "status", "elapsed_seconds", "task_id", "state", "error_type")}
    LOGGER.info(json.dumps({"event": name, **allowed}, ensure_ascii=False))


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("erp_support_http_requests", "Completed HTTP requests", ["method", "route", "status"], registry=self.registry)
        self.latency = Histogram("erp_support_http_duration_seconds", "HTTP duration", ["method", "route"], registry=self.registry)
        self.tasks = Gauge("erp_support_diagnosis_tasks", "Persisted diagnosis tasks by state", ["state"], registry=self.registry)

    def observe(self, method: str, route: str, status: int, elapsed: float) -> None:
        self.requests.labels(method, route, str(status)).inc()
        self.latency.labels(method, route).observe(elapsed)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def collect_tasks(self, store) -> None:
        state = records.c.payload["state"].as_string()
        with store.transaction() as db:
            counts = dict(db.execute(select(state, func.count()).where(records.c.kind == "task").group_by(state)).all())
        for value in ("queued", "running", "completed", "partial", "failed", "interrupted"):
            self.tasks.labels(value).set(counts.get(value, 0))


@contextmanager
def correlation(identifier: str):
    token = request_id_context.set(identifier)
    try:
        yield
    finally:
        request_id_context.reset(token)
