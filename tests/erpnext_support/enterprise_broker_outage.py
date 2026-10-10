"""Real owned broker outage and durable outbox; unavailable ERP fixture only."""

import argparse
import json
import time
import uuid
from pathlib import Path

import requests

from enterprise_worker_restart import ROOT, module
from holmes.erpnext_support.security import safe_path


def run(config: Path) -> dict:
    lab = module.Enterprise(config)
    resources = lab.resources()
    lab.guard(resources)
    if lab.config["mode"] != "enterprise":
        raise ValueError("Requires isolated enterprise fixture deployment")
    state = Path(lab.config["state_dir"])
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    if "https://erp-enterprise-fixture.invalid" not in settings["allowed_erp_origins"]:
        raise ValueError("Explicit unavailable fixture required")
    queue = next(c for c in resources["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "queue")
    gateway = next(c for c in resources["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "gateway")
    ports = gateway["NetworkSettings"]["Ports"]["8080/tcp"]
    assert ports and ports[0]["HostIp"] == "127.0.0.1" and ports[0]["HostPort"] == "8875"
    checks = []

    def check(name, condition):
        if not condition:
            failure = AssertionError(name)
            failure.completed_checks = list(checks)
            raise failure
        checks.append(name)

    base = "http://127.0.0.1:8875"
    with requests.Session() as api:
        api.trust_env = False
        login = api.post(base + "/api/session", json=json.loads((state / "initial-login.json").read_text(encoding="utf-8")), timeout=10)
        check("authenticated_before_broker_outage", login.status_code == 200)
        api.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        api.headers["X-Request-ID"] = "broker-outage-" + uuid.uuid4().hex
        ready = api.get(base + "/ready", timeout=10)
        check("dependencies_ready_before_fault", ready.status_code == 200)
        environment = api.post(base + "/api/environments", json={
            "name": "隔离不可用数据源：Broker断连演练", "base_url": "https://erp-enterprise-fixture.invalid", "credential_ref": "queue-fixture",
            "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "installed_apps": [], "allowed_services": [], "model_consent": False,
        }, timeout=10)
        check("unavailable_fixture_registered", environment.status_code == 200)
        ticket = api.post(base + "/api/tickets", json={
            "environment_id": environment.json()["id"], "delivery_note": "QA-BROKER-OUTAGE-ONLY",
            "description": "仅验证新企业项目的队列断连与持久待发恢复，不是真实ERP诊断。",
            "occurred_at": "2026-10-10T12:00:00+08:00",
        }, timeout=10)
        check("ticket_created_before_fault", ticket.status_code == 200)
        ticket_id = ticket.json()["id"]
        queue_stopped = False
        try:
            lab.guard(lab.resources())
            queue_stopped = True
            lab.docker(["stop", "--time", "5", queue["Id"]], timeout=25)
            readiness = api.get(base + "/ready", timeout=10)
            check("readiness_detected_broker_fault", readiness.status_code == 503 and readiness.json()["database_ready"] and not readiness.json()["broker_ready"])
            check("api_liveness_survived_fault", api.get(base + "/health", timeout=5).status_code == 200)
            accepted = api.post(base + "/api/tickets/" + ticket_id + "/diagnose", timeout=20)
            check("task_accepted_without_broker", accepted.status_code == 202)
            task_id = accepted.json()["task"]["id"]
            detail = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
            pending = next(t for t in detail["tasks"] if t["id"] == task_id)
            check("postgres_retained_queued_task", pending["state"] == "queued" and pending.get("publish_error") == "queue_publish_failed")
        finally:
            if queue_stopped:
                # Always recover the deliberately stopped, owned test broker.
                lab.guard(lab.resources())
                lab.docker(["start", queue["Id"]], timeout=25)
        restored_at = time.perf_counter()
        deadline = time.monotonic() + 150
        final = None
        ready = False
        while time.monotonic() < deadline:
            detail = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
            final = next(t for t in detail["tasks"] if t["id"] == task_id)
            ready = api.get(base + "/ready", timeout=10).status_code == 200
            if final["state"] not in ("queued", "running") and ready:
                break
            time.sleep(1)
        check("outbox_republished_and_worker_consumed", final and final["state"] == "partial")
        check("same_persisted_task_not_duplicated", len(detail["tasks"]) == 1 and final["id"] == task_id)
        check("dependency_readiness_restored", ready)
        check("failure_evidence_preserved", final["result"]["missing"] and final["result"]["tool_calls"])
        return {
            "layer": "real Docker/PostgreSQL/Valkey/Celery broker outage", "erp_fixture": "unavailable .invalid; not real ERP",
            "model_called": False, "production_connected": False, "checks": checks, "passed": len(checks),
            "project": lab.config["project"], "task_id": task_id, "task_state": final["state"],
            "recovery_seconds": time.perf_counter() - restored_at, "task_elapsed_seconds": final["elapsed_seconds"],
            "tool_calls": final["result"]["metrics"]["tool_calls"], "tool_success_rate": final["result"]["metrics"]["tool_success_rate"],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = safe_path(ROOT / "examples/erpnext-support", args.output)
    if output.exists():
        raise FileExistsError("Choose a new report path before running the fault exercise")
    started = time.perf_counter()
    try:
        result, code = run(args.config), 0
    except Exception as exc:
        result, code = {"status": "failed", "error_type": type(exc).__name__, "layer": "enterprise_broker_outage", "model_called": False}, 1
        completed = getattr(exc, "completed_checks", [])
        result.update(checks=completed, passed=len(completed))
        if isinstance(exc, AssertionError) and exc.args and isinstance(exc.args[0], str) and exc.args[0].replace("_", "").isalnum():
            result["failed_check"] = exc.args[0]
    result["elapsed_seconds"] = time.perf_counter() - started
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": "passed" if code == 0 else "failed", "report": str(output), "checks": result.get("passed", 0)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
