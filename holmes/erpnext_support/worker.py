"""Linux Celery worker. Only stored task identifiers cross the broker boundary."""

import json
import os
import threading
import uuid
from pathlib import Path

from celery.signals import worker_ready as ready_signal, worker_shutdown

from holmes.erpnext_support.jobs import client, run_task, worker_ready
from holmes.erpnext_support.observability import configure_logging
from holmes.erpnext_support.settings import EXAMPLE, Settings
from holmes.erpnext_support.storage import Store, utc_now


settings = Settings.load(Path(os.environ.get("ERP_SUPPORT_SETTINGS", EXAMPLE / ".runtime/workbench/settings.json")))
if settings.queue_backend != "celery":
    raise ValueError("独立Worker要求明确配置企业消息队列")
celery_app = client(settings)
configure_logging()
stop = threading.Event()
identity = uuid.uuid4().hex


@celery_app.task(name="erpnext_support.diagnose", ignore_result=True)
def diagnose_task(task_id: str):
    store = Store(settings.storage_target)
    try:
        return run_task(settings, store, task_id)
    finally:
        store.close()


@ready_signal.connect
def started(**kwargs):
    def heartbeat():
        store = Store(settings.storage_target)
        worker = store.add("worker", {"instance": identity, "heartbeat_at": utc_now()})
        try:
            while not stop.wait(10):
                store.update("worker", worker["id"], lambda w: w.update(heartbeat_at=utc_now()))
        finally:
            store.delete("worker", worker["id"])
            store.close()
    threading.Thread(target=heartbeat, daemon=True).start()


@worker_shutdown.connect
def stopped(**kwargs):
    stop.set()


if __name__ == "__main__":
    store = Store(settings.storage_target)
    try:
        available = worker_ready(store)
        print(json.dumps({"worker_ready": available}))
        raise SystemExit(0 if available else 1)
    finally:
        store.close()
