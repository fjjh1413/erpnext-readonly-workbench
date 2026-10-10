"""Real owned Docker/API/PostgreSQL/Celery smoke with unavailable ERP fixture.

This is not a real ERP or model accuracy test. No secret/session is reported.
"""

import argparse
import importlib.util
import json
import secrets
import time
import uuid
from pathlib import Path

import requests

from holmes.erpnext_support.security import safe_path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "examples/erpnext-support/enterprise/enterprise.py"
spec = importlib.util.spec_from_file_location("owned_enterprise", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def smoke(config: Path) -> dict:
    lab = module.Enterprise(config)
    resources = lab.resources()
    lab.guard(resources)
    gateway = next(c for c in resources["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "gateway")
    mapping = gateway["NetworkSettings"]["Ports"]["8080/tcp"]
    assert mapping and mapping[0]["HostIp"] == "127.0.0.1" and mapping[0]["HostPort"] == "8875"
    base = "http://127.0.0.1:8875"
    state = Path(lab.config["state_dir"])
    initial = json.loads((state / "initial-login.json").read_text(encoding="utf-8"))
    checks = []

    def check(name, condition):
        assert condition, name
        checks.append(name)

    with requests.Session() as api, requests.Session() as reviewer:
        api.trust_env = reviewer.trust_env = False
        check("liveness", api.get(base + "/health", timeout=5).json()["version"] == "2.0.0")
        ready = api.get(base + "/ready", timeout=10)
        check("postgres_broker_worker_ready", ready.status_code == 200 and ready.json()["ready"])
        login = api.post(base + "/api/session", json=initial, timeout=10)
        check("real_admin_login", login.status_code == 200)
        api.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        api.headers["X-Request-ID"] = "enterprise-smoke-" + uuid.uuid4().hex
        status = api.get(base + "/api/system/status", timeout=10).json()
        check("enterprise_backends", status["storage_backend"] == "postgresql" and status["queue_backend"] == "celery" and status["worker_ready"])
        username, password = "qa_" + uuid.uuid4().hex[:12], secrets.token_urlsafe(24)
        user = api.post(base + "/api/users", json={"username": username, "password": password, "role": "reviewer", "environment_ids": []}, timeout=10)
        check("create_account", user.status_code == 200 and "password" not in user.text)
        user_id = user.json()["id"]
        check("real_reviewer_login", reviewer.post(base + "/api/session", json={"username": username, "password": password}, timeout=10).status_code == 200)
        check("reviewer_audit_denied", reviewer.get(base + "/api/audit", timeout=5).status_code == 403)
        check("disable_account", api.patch(base + "/api/users/" + user_id, json={"disabled": True}, timeout=10).status_code == 200)
        check("old_session_revoked", reviewer.get(base + "/api/session", timeout=5).status_code == 401)
        check("password_reset", api.post(base + "/api/users/" + user_id + "/password", json={"password": secrets.token_urlsafe(24)}, timeout=10).status_code == 200)
        environment = api.post(base + "/api/environments", json={
            "name": "隔离不可用ERP数据源测试", "base_url": "https://erp-enterprise-fixture.invalid", "credential_ref": "queue-fixture",
            "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "installed_apps": [], "allowed_services": [], "model_consent": False,
        }, timeout=10)
        check("register_unavailable_fixture", environment.status_code == 200)
        environment_id = environment.json()["id"]
        credential_path = safe_path(state, state / "credentials/queue-fixture.json")
        if not credential_path.exists():
            with credential_path.open("x", encoding="utf-8") as output:
                json.dump({"api_key": secrets.token_hex(16), "api_secret": secrets.token_hex(16)}, output)
        ticket = api.post(base + "/api/tickets", json={"environment_id": environment_id, "delivery_note": "QA-UNAVAILABLE-ONLY",
            "description": "明确不可用的隔离数据源：仅验证真实任务队列与失败降级，不声明真实ERP诊断。", "occurred_at": "2026-10-10T12:00:00+08:00"}, timeout=10)
        check("persisted_ticket", ticket.status_code == 200)
        ticket_id = ticket.json()["id"]
        scheduled = api.post(base + "/api/tickets/" + ticket_id + "/diagnose", timeout=10)
        check("durable_task_accepted", scheduled.status_code == 202)
        task_id = scheduled.json()["task"]["id"]
        worker = next(c for c in resources["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "worker")
        code = 'import sys; from holmes.erpnext_support.worker import celery_app; celery_app.send_task("erpnext_support.diagnose",args=[sys.argv[1]],task_id=sys.argv[1]); print("duplicate_message_published")'
        lab.docker(["exec", worker["Id"], "python", "-c", code, task_id], timeout=120)
        deadline = time.monotonic() + 150
        task = None
        while time.monotonic() < deadline:
            detail = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
            task = next(t for t in detail["tasks"] if t["id"] == task_id)
            if task["state"] not in ("queued", "running"):
                break
            time.sleep(1)
        check("real_celery_consumed_task", task and task["state"] == "partial")
        result = task["result"]
        check("missing_source_explicit", bool(result["missing"]) and result["mode"] == "rules_degraded")
        check("actual_tool_calls_retained", bool(result.get("tool_calls")) and bool(task["steps"]))
        start, count = task["started_at"], len(task["steps"])
        time.sleep(2)
        after = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
        repeated = next(t for t in after["tasks"] if t["id"] == task_id)
        check("duplicate_did_not_repeat", repeated["started_at"] == start and len(repeated["steps"]) == count and len(after["tasks"]) == 1)
        check("human_record_only", api.post(base + "/api/tickets/" + ticket_id + "/actions", json={
            "description": "仅记录隔离队列回归操作；未修改ERP，也未确认发货完成。"}, timeout=10).status_code == 200)
        review = api.post(base + "/api/tickets/" + ticket_id + "/review", timeout=10)
        check("recheck_enqueued", review.status_code == 202)
        review_id = review.json()["task"]["id"]
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            latest = api.get(base + "/api/tickets/" + ticket_id, timeout=10).json()
            reviewed = next(t for t in latest["tasks"] if t["id"] == review_id)
            if reviewed["state"] not in ("queued", "running"):
                break
            time.sleep(1)
        check("recheck_preserves_missing_business_proof", reviewed["state"] == "partial" and reviewed["result"]["review"]["business_complete"] is False)
        check("cannot_claim_business_complete", api.post(base + "/api/tickets/" + ticket_id + "/close", json={
            "review_id": review_id, "confirmation": "不可用数据源缺少业务完成证据"}, timeout=10).status_code == 409)
        closed = api.post(base + "/api/tickets/" + ticket_id + "/close", json={
            "review_id": review_id, "manual_override": True,
            "confirmation": "这是隔离不可用数据源的流程回归，未确认发货完成，仅关闭本次测试工单。"}, timeout=10)
        check("explicit_test_exception_close", closed.status_code == 200 and closed.json()["manual_override"] is True)
        check("referenced_environment_not_deleted", api.delete(base + "/api/environments/" + environment_id, timeout=10).status_code == 409)
        audit = api.get(base + "/api/audit", params={"q": api.headers["X-Request-ID"], "limit": 2}, timeout=10).json()
        check("real_audit_filter_and_pagination", audit["total"] >= 2 and len(audit["items"]) == 2 and all(a["request_id"] == api.headers["X-Request-ID"] for a in audit["items"]))
        check("metrics_not_public", api.get(base + "/metrics", timeout=5).status_code == 404)
        return {"layer": "real owned Docker/API/PostgreSQL/Celery", "erp_fixture": "unavailable .invalid domain; not real ERP", "model_called": False,
                "project": lab.config["project"], "checks": checks, "passed": len(checks), "task_id": task_id,
                "task_state": task["state"], "task_elapsed_seconds": task["elapsed_seconds"], "tool_call_count": len(result["tool_calls"]),
                "tool_success_rate": result["metrics"]["tool_success_rate"],
                "diagnostic_result": {k: result[k] for k in ("mode", "confirmed_facts", "candidate_causes", "supporting_evidence", "missing_information", "next_steps", "tool_calls", "metrics")},
                "ticket_id": ticket_id, "environment_id": environment_id, "software": status}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = safe_path(ROOT / "examples/erpnext-support", args.output)
    if output.exists():
        raise FileExistsError("Choose a new report path before running the integration checks")
    started = time.perf_counter()
    try:
        result = smoke(args.config)
        code = 0
    except Exception as exc:
        result = {"layer": "enterprise_live_smoke", "status": "failed", "error_type": type(exc).__name__, "model_called": False}
        if isinstance(exc, AssertionError) and exc.args and isinstance(exc.args[0], str) and exc.args[0].replace("_", "").isalnum():
            result["failed_check"] = exc.args[0]
        code = 1
    result["elapsed_seconds"] = time.perf_counter() - started
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": "passed" if code == 0 else "failed", "report": str(output), "checks": result.get("passed", 0)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
