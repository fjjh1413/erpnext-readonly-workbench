"""Small dependency-free worker probe; no Agent/model imports."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from holmes.erpnext_support.settings import EXAMPLE, Settings
from holmes.erpnext_support.storage import Store


def main() -> int:
    try:
        settings = Settings.load(Path(os.environ.get("ERP_SUPPORT_SETTINGS", EXAMPLE / ".runtime/workbench/settings.json")))
        store = Store(settings.storage_target)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
            ready = any(value.get("heartbeat_at", "") > cutoff for value in store.list("worker", limit=100))
        finally:
            store.close()
    except Exception:
        ready = False
    print(json.dumps({"worker_ready": ready}))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
