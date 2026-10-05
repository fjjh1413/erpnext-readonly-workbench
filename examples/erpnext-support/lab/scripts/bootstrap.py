"""Creates only the fresh lab site and invokes its fixture-only seed module."""
import json
import os
import subprocess
import sys
from pathlib import Path

site = Path("sites/frontend")
owner = json.loads(Path("sites/holmes_lab_owner.json").read_text())
if owner != {"lab_id": os.environ["ERP_LAB_ID"]}:
    raise RuntimeError("The site volume does not belong to this lab")
if site.exists():
    marker = site / "private" / "holmes_seed_result.json"
    if marker.exists():
        result = json.loads(marker.read_text())
        if result.get("erpnext_version") != "15.85.1" or result.get("frappe_version") != "15.87.0":
            raise RuntimeError("The existing owned fixture has unsupported versions")
        print(json.dumps({"site": "frontend", "created": False, "seed_completed": True}))
        sys.exit(0)
    raise RuntimeError("Refusing to replace an existing site")
root = Path("/run/secrets/db_root_password").read_text().strip()
admin = Path("/run/secrets/admin_password").read_text().strip()
process = subprocess.run([
    "bench", "new-site", "frontend", "--no-mariadb-socket",
    "--db-root-username", "root", "--db-root-password", root,
    "--admin-password", admin, "--mariadb-user-host-login-scope", "%",
    "--install-app", "erpnext", "--set-default",
], check=False)
if process.returncode:
    # CalledProcessError would print its argv, including runtime credentials.
    raise RuntimeError("Fresh ERP site initialization failed")
subprocess.run(["/home/frappe/frappe-bench/env/bin/python", "/opt/lab/complete.py"], check=True)
print(json.dumps({"site": "frontend", "created": True, "seed_completed": True}))
