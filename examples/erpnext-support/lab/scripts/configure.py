"""One-time isolated benchmark configuration, not a diagnosis tool."""
import json
import os
from pathlib import Path

directory = Path("sites")
directory.mkdir(exist_ok=True)
owner_path = directory / "holmes_lab_owner.json"
owner = {"lab_id": os.environ["ERP_LAB_ID"]}
if owner_path.exists():
    if json.loads(owner_path.read_text()) != owner:
        raise RuntimeError("The site volume belongs to another lab")
else:
    with owner_path.open("x") as stream:
        json.dump(owner, stream)
(directory / "apps.txt").write_text("frappe\nerpnext\nholmes_readonly\n")
(directory / "common_site_config.json").write_text(json.dumps({
    "db_host": "db", "db_port": 3306,
    "redis_cache": "redis://redis-cache:6379", "redis_queue": "redis://redis-queue:6379",
    "redis_socketio": "redis://redis-queue:6379", "socketio_port": 9000,
    "serve_default_site": True, "gunicorn_workers": 2,
}))
