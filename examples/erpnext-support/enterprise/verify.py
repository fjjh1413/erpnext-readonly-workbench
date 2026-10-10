"""Read-only verification of a guarded deployment, without exposing secrets."""

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import requests

from enterprise import Enterprise, ROOT, checked_path


def verify(config: Path, context: Path) -> dict:
    started = time.perf_counter()
    lab = Enterprise(config)
    resources = lab.resources()
    lab.guard(resources)
    lab.guard_collisions()
    lab.preflight()
    containers = {item["Config"]["Labels"]["com.docker.compose.service"]: item for item in resources["containers"]}
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "passed": True})

    with requests.Session() as session:
        session.trust_env = False
        for endpoint in ("/health", "/ready"):
            check(endpoint.strip("/") + "_200", session.get("http://127.0.0.1:8875" + endpoint, timeout=5).status_code == 200)
        check("public_metrics_denied", session.get("http://127.0.0.1:8875/metrics", timeout=5).status_code == 404)
        index = session.get("http://127.0.0.1:8875/", timeout=5)
        assets = re.findall(r'(?:src|href)="(/assets/[A-Za-z0-9_.-]+)"', index.text)
        check("frontend_index_and_assets", index.status_code == 200 and len(assets) >= 2)
        check("compiled_assets_200", all(session.get("http://127.0.0.1:8875" + asset, timeout=5).status_code == 200 for asset in assets))

    queue = containers["queue"]
    denied = lab.docker(["exec", "--user", "999:999", queue["Id"], "sh", "-c",
                         'REDISCLI_AUTH="$(cat /run/secrets/queue_password)"; export REDISCLI_AUTH; '
                         'valkey-cli --user default CONFIG GET save'])
    check("queue_management_acl_denied", "NOPERM" in denied)

    db = containers["db"]
    role_query = ("SELECT json_build_object('superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,"
                  "'schema_create',has_schema_privilege(current_user,'public','CREATE'),"
                  "'audit_update',has_table_privilege(current_user,'audit_events','UPDATE'),"
                  "'audit_delete',has_table_privilege(current_user,'audit_events','DELETE'),"
                  "'audit_insert',has_table_privilege(current_user,'audit_events','INSERT')) "
                  "FROM pg_roles WHERE rolname=current_user")
    role_command = ('PGPASSWORD="$(cat /run/secrets/postgres_app_password)"; export PGPASSWORD; '
                    'psql --host=127.0.0.1 --username=workbench_app --dbname=holmes --no-psqlrc '
                    '--set=ON_ERROR_STOP=1 -At -c "' + role_query + '"')
    role = json.loads(lab.docker(["exec", "--user", "999:999", db["Id"], "sh", "-c", role_command]))
    check("app_role_no_superuser_or_ddl", all(role[key] is False for key in ("superuser", "createdb", "createrole", "schema_create")))
    check("audit_append_permissions_only", role["audit_insert"] is True and role["audit_update"] is False and role["audit_delete"] is False)
    check("api_worker_have_no_bootstrap_or_migration_secret", all(
        not any("postgres_root_password" in mount["Destination"] or "migration_database_url" in mount["Destination"]
                for mount in containers[name]["Mounts"]) for name in ("api", "worker")))

    prometheus = containers["prometheus"]
    targets = json.loads(lab.docker(["exec", "--user", "65534:65534", prometheus["Id"], "wget", "-q", "-O", "-",
                                   "http://127.0.0.1:9090/api/v1/targets"]))
    check("prometheus_authenticated_scrape", any(target.get("health") == "up" for target in targets.get("data", {}).get("activeTargets", [])))
    task_gauge = json.loads(lab.docker(["exec", "--user", "65534:65534", prometheus["Id"], "wget", "-q", "-O", "-",
                                      "http://127.0.0.1:9090/api/v1/query?query=erp_support_diagnosis_tasks"]))
    gauge_values = task_gauge.get("data", {}).get("result", [])
    check("prometheus_persisted_task_gauge", task_gauge.get("status") == "success" and bool(gauge_values)
          and all(value.get("metric", {}).get("state") and float(value["value"][1]) >= 0 for value in gauge_values))
    api = containers["api"]
    unauthenticated = json.loads(lab.docker(["exec", "--user", "10001:10001", api["Id"], "python", "-c",
        'import urllib.request,urllib.error,json; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); '
        'r=urllib.request.Request("http://127.0.0.1:8765/metrics",headers={"Host":"localhost"}); '
        '\ntry: x=o.open(r,timeout=3); print(json.dumps({"status":x.status}))'
        '\nexcept urllib.error.HTTPError as e: print(json.dumps({"status":e.code}))']))
    check("internal_metrics_auth_required", unauthenticated["status"] in (401, 403))
    relay_status = json.loads(lab.docker(["exec", "--user", "10001:10001", api["Id"], "python", "-c",
        'import urllib.request,urllib.error,json; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); result={}; '
        '\nfor method in ("GET","POST"):'
        '\n r=urllib.request.Request("http://gateway:9091/metrics",method=method)'
        '\n try: x=o.open(r,timeout=3); result[method]=x.status'
        '\n except urllib.error.HTTPError as e: result[method]=e.code'
        '\nprint(json.dumps(result))']))
    check("internal_relay_get_requires_token", relay_status["GET"] == 403)
    check("internal_relay_rejects_non_get", relay_status["POST"] in (403, 405))
    check("migration_completed", containers["migrate"]["State"]["Status"] == "exited" and containers["migrate"]["State"]["ExitCode"] == 0)
    check("six_services_healthy", all(containers[name]["State"].get("Health", {}).get("Status") == "healthy" for name in ("db", "queue", "api", "worker", "gateway", "prometheus")))

    manifest = json.loads((checked_path(context) / "build-manifest.json").read_text())
    expected = {path[7:]: digest for path, digest in manifest.items() if path.startswith("holmes/")}
    actual = json.loads(lab.docker(["exec", "--user", "10001:10001", api["Id"], "python", "-c",
        'import pathlib,hashlib,json; p=pathlib.Path("/app/holmes"); print(json.dumps({x.relative_to(p).as_posix():hashlib.sha256(x.read_bytes()).hexdigest() for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts and x.suffix!=".pyc"}))']))
    check("container_source_matches_build_context", actual == expected)
    root = ROOT.parents[2]
    runtime_source = {path: digest for path, digest in manifest.items() if path.startswith("holmes/") and Path(path).suffix == ".py"}
    check("frozen_python_source_matches_workspace", all(hashlib.sha256((root / path).read_bytes()).hexdigest() == digest for path, digest in runtime_source.items()))
    frontend_inputs = {path: digest for path, digest in manifest.items() if path.startswith("frontend/")}
    check("frozen_frontend_matches_workspace", all(hashlib.sha256((root / "examples/erpnext-support" / path).read_bytes()).hexdigest() == digest for path, digest in frontend_inputs.items()))
    versions = json.loads(lab.docker(["exec", "--user", "10001:10001", api["Id"], "python", "-c",
        'import importlib.metadata,json,sys; print(json.dumps({"python":sys.version.split()[0],**{n:importlib.metadata.version(n) for n in ["celery","redis","alembic","prometheus-client","SQLAlchemy","pg8000","tzdata"]}}))']))
    return {"layer": "real owned Docker read-only deployment verification", "model_called": False,
            "erp_connected": False, "project": lab.config["project"], "image": lab.config["image"],
            "checks": checks, "passed": len(checks), "versions": versions, "assets": assets,
            "internal_relay_status": relay_status,
            "task_gauge_states": sorted(value["metric"]["state"] for value in gauge_values),
            "migration": {"status": containers["migrate"]["State"]["Status"], "exit_code": containers["migrate"]["State"]["ExitCode"],
                          "historical_container_health": containers["migrate"]["State"].get("Health", {}).get("Status")},
            "source_manifest_files": len(manifest), "python_runtime_files": len(runtime_source),
            "frontend_build_inputs": len(frontend_inputs), "elapsed_seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = checked_path(args.output)
    if output.exists():
        raise ValueError("A new report path is required")
    result = verify(args.config, args.context)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"passed": result["passed"], "report": str(output)}))


if __name__ == "__main__":
    main()
