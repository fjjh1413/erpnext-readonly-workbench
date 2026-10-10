"""Real owned worker interruption; the slow HTTP source is an explicit mock.

Only the new enterprise worker is killed. No ERP, model or production is used.
"""

import argparse
import importlib.util
import json
import time
import uuid
from pathlib import Path

import requests

from holmes.erpnext_support.security import safe_path


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("owned_enterprise_restart", ROOT / "examples/erpnext-support/enterprise/enterprise.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
MOCK_ORIGIN = "http://127.0.0.1:19090"


def run(config: Path) -> dict:
    lab = module.Enterprise(config)
    resources = lab.resources()
    lab.guard(resources)
    if lab.config["mode"] != "enterprise":
        raise ValueError("Requires the new isolated enterprise fixture deployment")
    state = Path(lab.config["state_dir"])
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    if MOCK_ORIGIN not in settings["allowed_erp_origins"] or "https://erp-enterprise-fixture.invalid" not in settings["allowed_erp_origins"]:
        raise ValueError("Explicit isolated fixture origins required")
    worker = next(c for c in resources["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "worker")
    gateway = next(c for c in resources["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "gateway")
    ports = gateway["NetworkSettings"]["Ports"]["8080/tcp"]
    assert ports and ports[0]["HostIp"] == "127.0.0.1" and ports[0]["HostPort"] == "8875"
    worker_id = worker["Id"]
    marker = "/tmp/erp-enterprise-" + uuid.uuid4().hex
    # Standard-library, GET-only mock inside the owned worker. It dies with it.
    source = (
        "import json,time\nfrom pathlib import Path\nfrom http.server import BaseHTTPRequestHandler,HTTPServer\n"
        "class Slow(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  if self.path.split('?',1)[0] == '/api/method/holmes_readonly.api.capabilities':\n"
        "   body=json.dumps({'message':{'erpnext_version':'15.85.1','frappe_version':'15.87.0','installed_apps':[],'read_permissions':{},'site_timezone':'Asia/Shanghai','limitations':['Explicit local mock; not actual ERP version evidence']}}).encode()\n"
        "   self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return\n"
        f"  Path({marker!r}).write_text('GET received')\n"
        "  time.sleep(30)\n  self.send_response(503)\n  self.end_headers()\n"
        " def log_message(self,*args): pass\n"
        "HTTPServer(('127.0.0.1',19090),Slow).serve_forever()\n"
    )
    lab.docker(["exec", "-d", worker_id, "python", "-c", source], timeout=15)
    checks = []

    def check(name, condition):
        if not condition:
            failure = AssertionError(name)
            failure.completed_checks = list(checks)
            raise failure
        checks.append(name)

    # Wait for this exact fixed fixture before enqueuing. This prevents the
    # initial capability read racing the detached server startup.
    preflight = (
        "import http.client,json,time\n"
        "for attempt in range(10):\n"
        " try:\n"
        "  connection=http.client.HTTPConnection('127.0.0.1',19090,timeout=0.5)\n"
        "  connection.request('GET','/api/method/holmes_readonly.api.capabilities')\n"
        "  response=connection.getresponse(); data=json.loads(response.read(4097)); connection.close()\n"
        "  if response.status==200 and data.get('message',{}).get('limitations')==['Explicit local mock; not actual ERP version evidence']:\n"
        "   print('fixture_ready'); break\n"
        " except Exception: pass\n"
        " time.sleep(0.2)\n"
        "else: raise SystemExit(1)\n"
    )
    check("owned_get_fixture_ready", lab.docker(["exec", worker_id, "python", "-c", preflight], timeout=15).strip() == "fixture_ready")

    base = "http://127.0.0.1:8875"
    with requests.Session() as api:
        api.trust_env = False
        account = json.loads((state / "initial-login.json").read_text(encoding="utf-8"))
        login = api.post(base + "/api/session", json=account, timeout=10)
        check("authenticated_isolated_fixture", login.status_code == 200)
        api.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        api.headers["X-Request-ID"] = "worker-restart-" + uuid.uuid4().hex
        environment = api.post(base + "/api/environments", json={
            "name": "隔离慢响应Mock：Worker中断演练", "base_url": MOCK_ORIGIN, "credential_ref": "queue-fixture",
            "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "installed_apps": [],
            "allowed_services": [], "model_consent": False,
        }, timeout=10)
        check("explicit_mock_source_registered", environment.status_code == 200)
        ticket = api.post(base + "/api/tickets", json={
            "environment_id": environment.json()["id"], "delivery_note": "MOCK-WORKER-INTERRUPTION",
            "description": "慢响应只读Mock用于真实Worker中断演练，无真实ERP或业务修复。",
            "occurred_at": "2026-10-10T12:00:00+08:00",
        }, timeout=10)
        check("task_ticket_persisted", ticket.status_code == 200)
        ticket_id = ticket.json()["id"]
        scheduled = api.post(base + "/api/tickets/" + ticket_id + "/diagnose", timeout=10)
        check("durable_message_accepted", scheduled.status_code == 202)
        task_id = scheduled.json()["task"]["id"]
        deadline = time.monotonic() + 60
        running = None
        hit = False
        while time.monotonic() < deadline:
            detail = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
            running = next(t for t in detail["tasks"] if t["id"] == task_id)
            if running["state"] == "running" and running["steps"]:
                out = lab.docker(["exec", worker_id, "python", "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1]).exists())", marker], timeout=15)
                hit = "True" in out
                if hit:
                    break
            if running["state"] not in ("queued", "running"):
                break
            time.sleep(0.3)
        check("real_worker_entered_mock_get", running and running["state"] == "running" and hit)
        step_count = len(running["steps"])
        check("completed_mock_step_persisted_before_fault", step_count > 0)
        lab.guard(lab.resources())
        killed_at = time.perf_counter()
        try:
            lab.docker(["kill", "--signal=KILL", worker_id], timeout=20)
        finally:
            # Also recover if the Docker client timed out after killing the
            # process. Guard and restore only this owned worker.
            time.sleep(1)
            state_after_kill = next(c for c in lab.resources()["containers"] if c["Id"] == worker_id)
            operator_restart = not state_after_kill["State"]["Running"] and not state_after_kill["State"]["Restarting"]
            if operator_restart:
                lab.guard(lab.resources())
                lab.docker(["start", worker_id], timeout=20)
        # API maintenance fences the old task regardless of the restart path.
        deadline = time.monotonic() + 190
        terminal = None
        ready = False
        while time.monotonic() < deadline:
            detail = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
            terminal = next(t for t in detail["tasks"] if t["id"] == task_id)
            response = api.get(base + "/ready", timeout=10)
            ready = response.status_code == 200 and response.json()["worker_ready"]
            if terminal["state"] == "interrupted" and ready:
                break
            time.sleep(1)
        check("expired_lease_marked_interrupted", terminal and terminal["state"] == "interrupted")
        check("ticket_released_from_investigating", detail["status"] == "needs_information")
        check("completed_steps_preserved", len(terminal["steps"]) == step_count)
        check("restarted_worker_heartbeat_ready", ready)
        current = lab.resources()
        lab.guard(current)
        restarted = next(c for c in current["containers"] if c["Id"] == worker_id)
        check("only_owned_worker_restarted", restarted["State"]["Running"] and restarted["State"]["StartedAt"] != worker["State"]["StartedAt"])
        return {
            "layer": "real Docker/PostgreSQL/Celery worker interruption", "erp_source": "slow GET-only local mock",
            "mock_fixture_preflight": True,
            "model_called": False, "production_connected": False, "checks": checks, "passed": len(checks),
            "project": lab.config["project"], "task_id": task_id, "state": terminal["state"],
            "steps_preserved": step_count, "recovery_seconds": time.perf_counter() - killed_at,
            "completed_tool_calls": step_count,
            "completed_mock_tool_success_rate": sum(step["status"] == "success" for step in terminal["steps"]) / step_count,
            "worker_restart_count": restarted["RestartCount"],
            "test_operator_restart": operator_restart,
            "note": "No automatic task replay or ERP repair; interrupted investigation can be retried explicitly.",
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
        result, code = {"status": "failed", "error_type": type(exc).__name__, "layer": "enterprise_worker_restart", "model_called": False}, 1
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
