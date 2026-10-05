"""No Docker calls: ownership and manifest rejection tests for fixture writes."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "lab.py"
SPEC = importlib.util.spec_from_file_location("erp_fixture_lab", PATH)
lab_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab_module)


@pytest.fixture
def lab():
    obj = object.__new__(lab_module.ERPLab)
    obj.project = "holmes-erp-" + "a" * 12
    obj.lab_id = "b" * 32
    obj.secret_dir = lab_module.ROOT / ".runtime" / obj.project / "secrets"
    obj.state = {"owned": True, "closed": False, "containers": ["own"],
                 "networks": ["network"], "volumes": [obj.project + "_sites"]}
    return obj


@pytest.fixture
def manifest(lab):
    labels = {lab_module.LABEL: lab.lab_id}
    services = {}
    for name in lab_module.SERVICES:
        image = "mariadb:10.6.22" if name == "db" else "redis:6.2.18-alpine" if name.startswith("redis-") else "frappe/erpnext:v15.85.1"
        services[name] = {"image": image, "labels": labels.copy(),
                          "networks": {"core": {}}, "volumes": []}
    services["db"]["secrets"] = [{"source": "db_root_password"}]
    services["create-site"]["secrets"] = [{"source": name} for name in ("db_root_password", "admin_password")]
    services["frontend"]["networks"]["entry"] = {}
    services["frontend"]["ports"] = [{"host_ip": "127.0.0.1", "target": 8080, "published": "8090"}]
    return {"services": services,
            "networks": {name: {"name": lab.project + "_" + name, "labels": labels.copy(),
                                "internal": name == "core"} for name in ("core", "entry")},
            "volumes": {name: {"name": lab.project + "_" + name, "labels": labels.copy()}
                        for name in ("sites", "logs", "db-data", "redis-data")},
            "secrets": {name: {"file": str(lab.secret_dir / name)} for name in ("db_root_password", "admin_password")}}


def validate(lab, manifest):
    lab.compose = lambda args: json.dumps(manifest)
    lab.validate_manifest()


def test_expected_manifest(lab, manifest):
    validate(lab, manifest)


@pytest.mark.parametrize("change", [
    lambda m: m["services"]["backend"].update(privileged=True),
    lambda m: m["services"]["backend"].update(network_mode="host"),
    lambda m: m["services"]["backend"]["networks"].update(entry={}),
    lambda m: m["services"]["backend"].update(image="frappe/erpnext:latest"),
    lambda m: m["services"]["db"].update(ports=[{"target": 3306}]),
    lambda m: m["services"]["frontend"]["ports"][0].update(host_ip="0.0.0.0"),
    lambda m: m["services"]["frontend"]["ports"][0].update(published="8080"),
    lambda m: m["services"]["backend"].update(secrets=[{"source": "admin_password"}]),
    lambda m: m["services"]["backend"].update(volumes=[{"type": "bind", "source": "/var/run/docker.sock", "read_only": True}]),
    lambda m: m["services"]["backend"].update(volumes=[{"type": "volume", "source": "customer-data"}]),
    lambda m: m["networks"]["core"].update(internal=False),
    lambda m: m["networks"]["entry"].update(external=True),
    lambda m: m["volumes"]["sites"].update(external=True),
    lambda m: m["volumes"]["sites"].update(driver_opts={"type": "nfs"}),
    lambda m: m["secrets"]["admin_password"].update(file="C:/outside/password"),
])
def test_manifest_rejects_unsafe_changes(lab, manifest, change):
    altered = copy.deepcopy(manifest)
    change(altered)
    with pytest.raises(lab_module.ScopeError):
        validate(lab, altered)


def test_existing_resources_cannot_be_adopted(lab):
    lab.state["owned"] = False
    lab.resources = lambda: {"containers": ["own"], "networks": [], "volumes": []}
    with pytest.raises(lab_module.ScopeError, match="adopted"):
        lab.check()


def test_new_foreign_id_rejected_even_with_same_project(lab):
    lab.resources = lambda: {"containers": ["foreign"], "networks": [], "volumes": []}
    with pytest.raises(lab_module.ScopeError, match="Unrecorded"):
        lab.check()


def test_foreign_network_member_rejected(lab):
    lab.resources = lambda: {"containers": ["own"], "networks": ["network"], "volumes": []}
    lab.validate_resource = lambda kind, item: None
    lab.inspected = lambda kind, ids: [{"Containers": {"foreign": {}}}] if kind == "networks" else [{}]
    with pytest.raises(lab_module.ScopeError, match="Foreign container"):
        lab.check()


def test_closed_runtime_rejected(lab):
    lab.state["closed"] = True
    with pytest.raises(lab_module.ScopeError, match="closed"):
        lab.check()


def test_parent_traversal_rejected():
    with pytest.raises(lab_module.ScopeError, match="outside"):
        lab_module.safe_path(lab_module.ROOT / ".." / ".." / "outside.json")


def test_existing_file_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(lab_module, "ROOT", tmp_path)
    target = tmp_path / "evidence.json"
    target.write_text("preserve")
    with pytest.raises(FileExistsError):
        lab_module.write_new(target, {})
    assert target.read_text() == "preserve"


def test_unknown_fault_never_mutates(lab):
    lab.mutate = lambda args: pytest.fail("an invalid fault reached Docker")
    with pytest.raises(lab_module.ScopeError):
        lab.fault("delete_customer_stock")
