"""Opt-in real collector + original tools, with scoped isolated ERP faults.

These tests stop only the explicitly owned lab services and always restore.
They do not call a model, mutate business documents, or touch other projects.
"""

import importlib.util
import json
import os
import re
import time
import uuid
from pathlib import Path

import pytest
import requests

from holmes.erpnext_support.engine import diagnose
from holmes.erpnext_support.settings import EXAMPLE, Settings
from holmes.erpnext_support.security import sanitize, safe_path


@pytest.fixture(scope="module")
def runtime_lab():
    if os.environ.get("ERP_SUPPORT_RUN_RUNTIME_LIVE") != "1":
        pytest.skip("Actual runtime fault tests need explicit isolated lab opt-in")
    project = os.environ.get("ERP_SUPPORT_LIVE_PROJECT", "")
    assert re.fullmatch(r"holmes-erp-[a-f0-9]{12}", project)
    config = safe_path(EXAMPLE / "lab/.runtime", EXAMPLE / "lab/.runtime" / project / "config.json")
    spec = importlib.util.spec_from_file_location("erp_owned_test_lifecycle", EXAMPLE / "lab/lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lab = module.ERPLab(config)
    lab.check()
    settings = Settings.load(EXAMPLE / ".runtime/workbench/settings.json")
    credentials = settings.credentials("erp-lab")
    collector = settings.collector_credentials("erp-lab")
    seed = json.loads((config.parent / "seed-result.json").read_text())
    note = seed["documents"]["normal"]
    assert "http://127.0.0.1:8090" in settings.allowed_erp_origins
    environment = {"base_url": "http://127.0.0.1:8090", "credential_ref": "erp-lab", "collector_ref": "erp-lab",
                   "allowed_services": ["application", "database", "redis"], "model_consent": False,
                   "erpnext_version": "15.85.1", "frappe_version": "15.87.0"}
    ticket = {"delivery_note": note["name"], "description": "独立实验运行依赖故障诊断", "occurred_at": note["posting_date"] + "T10:00:00+08:00"}
    observations = []
    yield lab, settings, credentials, collector, environment, ticket, observations
    report = {"layer": "C real isolated Docker faults + actual ERP/collector/original Tool.invoke", "model": "not_called",
              "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "project": project,
              "observations": observations, "note": "Faults restored in finally; elapsed and call rates are actual observations, not model accuracy."}
    path = EXAMPLE / "reports" / ("runtime-live-" + uuid.uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as output:
        json.dump(sanitize(report, [*credentials.values(), collector["token"]]), output, ensure_ascii=False, indent=2)
    print("Actual runtime evidence artifact:", path)


def run_diagnosis(fixture):
    lab, settings, credentials, collector, environment, ticket, observations = fixture
    steps = []
    result = diagnose(settings, environment, ticket, credentials, lambda: True, steps.append, [])
    assert result["mode"] == "rules_degraded"
    assert result["model_usage"]["status"] == "not_called"
    assert len(steps) == len(result["tool_calls"])
    return result


@pytest.mark.erp_live
def test_actual_healthy_runtime_does_not_replace_business_proof(runtime_lab):
    result = run_diagnosis(runtime_lab)
    runtime_lab[-1].append({"scenario": "healthy", "result": result})
    runtime = next(e for e in result["evidence"] if e["tool_name"] == "erp_runtime_health")
    assert runtime["status"] == "success"
    assert runtime["payload"]["services"]["db"]["health"] == "healthy"
    assert runtime["payload"]["services"]["redis-cache"]["ping"] == "PONG"
    assert result["business_state"]["business_complete"] is True
    assert not any(c["code"].startswith("runtime_") for c in result["causes"])


@pytest.mark.erp_live
def test_actual_collector_rejects_unauthorized_and_free_query(runtime_lab):
    collector = runtime_lab[3]
    with requests.Session() as client:
        client.trust_env = False
        first = client.get(collector["base_url"] + "/snapshot", timeout=3)
        second = client.get(collector["base_url"] + "/snapshot?command=restart", headers={"Authorization": "Bearer " + collector["token"]}, timeout=3)
    runtime_lab[-1].append({"scenario": "collector_restrictions", "unauthorized_status": first.status_code, "query_status": second.status_code})
    assert first.status_code == 403 and second.status_code == 403


@pytest.mark.erp_live
@pytest.mark.parametrize("fault,service,cause", [
    ("database_unavailable", "db", "runtime_database"),
    ("cache_unavailable", "redis-cache", "runtime_redis"),
    ("api_unavailable", "backend", "runtime_application"),
])
def test_actual_dependency_fault_is_cited_candidate_and_restored(runtime_lab, fault, service, cause):
    lab = runtime_lab[0]
    try:
        lab.fault(fault)
        result = run_diagnosis(runtime_lab)
        runtime_lab[-1].append({"scenario": fault, "result": result})
        evidence = next(e for e in result["evidence"] if e["tool_name"] == "erp_runtime_health")
        assert evidence["status"] == "success"
        assert evidence["payload"]["services"][service]["state"] == "exited"
        candidate = next(c for c in result["causes"] if c["code"] == cause)
        assert candidate["status"] == "candidate" and candidate["evidence_ids"] == [evidence["id"]]
        assert result["business_state"]["business_complete"] is False
    finally:
        lab.restore()
        deadline = time.monotonic() + 45
        ready = False
        with requests.Session() as client:
            client.trust_env = False
            while time.monotonic() < deadline:
                try:
                    response = client.get("http://127.0.0.1:8090/api/method/ping", timeout=2)
                    if response.status_code == 200 and response.json() == {"message": "pong"}:
                        ready = True
                        break
                except (requests.RequestException, ValueError):
                    pass
                time.sleep(1)
        runtime_lab[-1].append({"scenario": fault + "_restore", "api_ready": ready, "states": lab.status()["services"]})
        assert ready, "Own ERP did not become ready after restoration"
