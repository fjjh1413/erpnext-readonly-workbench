"""Opt-in lifecycle and denied-write checks in a newly generated isolated lab.

The test harness attempts writes to prove fixture permissions deny them. These
operations are never registered in, or available to, the diagnostic Agent.
"""

import importlib
import json
import os
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "docker-readonly"
# A constant fixture-only check; no input can select hosts, SQL, code or keys.
PERMISSION_CHECK = """
import json
import pymysql
import redis
from probe import password
result = {}
with pymysql.connect(host='mysql', port=3306, user='probe',
        password=password('mysql_probe_password'), connect_timeout=2,
        read_timeout=2, write_timeout=2) as connection:
    try:
        with connection.cursor() as cursor:
            cursor.execute('CREATE DATABASE holmes_readonly_guard')
        result['mysql_write_denied'] = False
    except pymysql.MySQLError as error:
        result['mysql_error_code'] = error.args[0]
        result['mysql_write_denied'] = error.args[0] in (1044, 1142)
with redis.Redis(host='redis', port=6379, username='probe',
        password=password('redis_probe_password'), socket_connect_timeout=2,
        socket_timeout=2, protocol=2) as connection:
    try:
        connection.set('holmes-readonly-guard', 'guard')
        result['redis_write_denied'] = False
    except redis.exceptions.NoPermissionError:
        result['redis_write_denied'] = True
print(json.dumps(result))
"""


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("HOLMES_READONLY_LIVE") != "1",
                    reason="Requires an explicitly enabled fresh isolated Docker lab")
def test_real_permissions_and_three_fault_restores(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    lab_module = importlib.import_module("lab")
    config = lab_module.create_lab()
    lab = lab_module.ComposeLab(config)
    results = {"project": config["project"], "restores": [], "cleanup_completed": False}
    destination = Path(os.environ["HOLMES_READONLY_RESTORE_REPORT"]) if os.environ.get(
        "HOLMES_READONLY_RESTORE_REPORT") else tmp_path / "actual-permissions-restore.json"
    assert not destination.exists(), "Refusing to overwrite a report"
    try:
        lab.up()
        identifier = lab._probe_container()  # verifies recorded ownership and fixed image/mounts
        permission = json.loads(lab._run([
            "exec", "--user", "10001:10001", identifier, "python", "-c", PERMISSION_CHECK,
        ], timeout=10))
        results["permissions"] = permission
        assert permission["mysql_write_denied"] is True
        assert permission["redis_write_denied"] is True
        for scenario in lab_module.FAULTS:
            lab.fault(scenario)
            lab.restore()
            observed = lab._ready_request()
            results["restores"].append({"fault": scenario, "readiness": observed})
            assert observed == {"status_code": 200, "body": {
                "status": "ready", "dependencies": {"mysql": "ok", "redis": "ok"}}}
    finally:
        try:
            lab.close()
            results["cleanup_completed"] = True
        finally:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(results, stream, ensure_ascii=False, indent=2)
