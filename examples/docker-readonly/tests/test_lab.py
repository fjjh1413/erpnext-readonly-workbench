"""Lifecycle ownership tests. Docker is never contacted by these unit tests."""

import copy
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location("readonly_lab", Path(__file__).parents[1] / "lab.py")
assert SPEC and SPEC.loader
lab_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab_module)


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(lab_module, "EXAMPLE_DIR", tmp_path)
    monkeypatch.setattr(lab_module, "_restrict_directory", lambda path: path.chmod(0o700))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return lab_module.ComposeLab(lab_module.create_lab())


def manifest_for(lab: Any) -> dict[str, Any]:
    root = lab_module.EXAMPLE_DIR
    binds = {
        "api": [],
        "probe": [],
        "mysql": [
            ("mysql/init-probe.sh", "/docker-entrypoint-initdb.d/10-probe.sh"),
            ("mysql/healthcheck.sh", "/opt/lab/healthcheck.sh"),
        ],
        "redis": [
            ("redis/start-redis.sh", "/opt/lab/start-redis.sh"),
            ("redis/healthcheck.sh", "/opt/lab/healthcheck.sh"),
        ],
    }
    services = {}
    for service in ("api", "mysql", "redis", "probe"):
        volumes = [{"type": "bind", "source": str((root / source).resolve()),
                    "target": destination, "read_only": True}
                   for source, destination in binds[service]]
        if service == "mysql":
            volumes.append({"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"})
        services[service] = {
            "image": {"api": f"{lab.project}-api:lab", "probe": f"{lab.project}-api:lab",
                      "mysql": "mysql:8.4.4", "redis": "redis:7.4.2-alpine"}[service],
            "labels": {lab_module.LAB_LABEL: lab.lab_id},
            "networks": {"lab": {}},
            "volumes": volumes,
        }
    services["api"]["build"] = {"context": str(root / "app")}
    services["probe"].update({"read_only": True,
                              "command": ["python", "-c", "import signal; signal.pause()"],
                              "secrets": [{"source": "mysql_probe_password"}, {"source": "redis_probe_password"}]})
    return {
        "services": services,
        "networks": {"lab": {"internal": True, "name": f"{lab.project}_lab",
                             "labels": {lab_module.LAB_LABEL: lab.lab_id}}},
        "volumes": {"mysql_data": {"name": f"{lab.project}_mysql_data",
                                  "labels": {lab_module.LAB_LABEL: lab.lab_id}}},
        "secrets": {name: {"file": str(lab.secret_dir / name)} for name in lab_module.PASSWORD_FILES},
    }


def resource_for(lab: Any, kind: str) -> dict[str, Any]:
    labels = {lab_module.LAB_LABEL: lab.lab_id, lab_module.PROJECT_LABEL: lab.project}
    if kind == "containers":
        return {"Name": f"/{lab.project}-api-1", "Config": {"Labels": {
                    **labels, lab_module.SERVICE_LABEL: "api"}},
                "NetworkSettings": {"Networks": {f"{lab.project}_lab": {}}}}
    if kind == "networks":
        return {"Name": f"{lab.project}_lab", "Labels": labels, "Internal": True}
    return {"Name": f"{lab.project}_mysql_data", "Labels": labels}


def test_new_runtime_uses_distinct_generated_secrets_and_never_adopts_resources(lab: Any) -> None:
    assert set(lab.config) == {"project", "lab_id", "secret_dir"}
    assert re.fullmatch(r"holmes-lab-[a-f0-9]{12}", lab.project)
    assert re.fullmatch(r"[a-f0-9]{32}", lab.lab_id)
    assert lab.secret_dir == lab.directory / "secrets"
    assert len(set(lab._passwords)) == 3
    assert all(re.fullmatch(r"[a-f0-9]{64}", value) for value in lab._passwords)
    assert lab.state["owned"] is False
    assert not any(lab.state[kind] for kind in ("containers", "networks", "volumes"))
    assert all(value not in lab.config_path.read_text() for value in lab._passwords)


def test_create_collision_preserves_existing_files(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    before = lab.config_path.read_bytes()
    passwords = list(lab._passwords)
    monkeypatch.setattr(lab_module.secrets, "token_hex", lambda count: lab.lab_id)
    with pytest.raises(FileExistsError):
        lab_module.create_lab()
    assert lab.config_path.read_bytes() == before
    assert lab._passwords == passwords


def test_forged_config_cannot_adopt_a_project(lab: Any) -> None:
    (lab.directory / "ownership.json").unlink()
    with pytest.raises(lab_module.LabError, match="ownership record"):
        lab_module.ComposeLab(lab.config)


@pytest.mark.parametrize("kind", ["containers", "networks", "volumes"])
def test_existing_resources_are_rejected_even_with_matching_labels(
    lab: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    resources = {"containers": [], "networks": [], "volumes": []}
    resources[kind] = ["already-existed"]
    monkeypatch.setattr(lab, "_resources", lambda: resources)
    with pytest.raises(lab_module.LabError, match="never adopted"):
        lab._assert_owned()


def test_unrecorded_resources_are_rejected(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    lab.state["owned"] = True
    lab.state["containers"] = ["recorded"]
    monkeypatch.setattr(lab, "_resources", lambda: {
        "containers": ["injected"], "networks": [], "volumes": []
    })
    with pytest.raises(lab_module.LabError, match="not created and recorded"):
        lab._assert_owned()


@pytest.mark.parametrize("kind", ["containers", "networks", "volumes"])
def test_owned_resources_require_both_labels(lab: Any, kind: str) -> None:
    resource = resource_for(lab, kind)
    lab._validate_resource(kind, resource)
    labels = resource["Config"]["Labels"] if kind == "containers" else resource["Labels"]
    labels[lab_module.LAB_LABEL] = "different-lab"
    with pytest.raises(lab_module.LabError, match="matching project and lab labels"):
        lab._validate_resource(kind, resource)


def test_container_joined_to_another_network_is_rejected(lab: Any) -> None:
    resource = resource_for(lab, "containers")
    resource["NetworkSettings"]["Networks"]["production"] = {}
    with pytest.raises(lab_module.LabError, match="outside the lab network"):
        lab._validate_resource("containers", resource)


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://server", "npipe://remote/pipe/docker_engine"])
def test_remote_or_forwarded_context_is_rejected(lab: Any, monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setattr(lab, "_run", lambda *args, **kwargs: json.dumps([
        {"Name": "candidate", "Endpoints": {"docker": {"Host": endpoint}}}
    ]))
    with pytest.raises(lab_module.LabError, match="local Docker socket"):
        lab._check_local_daemon()


@pytest.mark.parametrize("endpoint", ["unix:///var/run/docker.sock", "npipe:////./pipe/dockerDesktopLinuxEngine"])
def test_local_context_is_pinned(lab: Any, monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setattr(lab, "_run", lambda *args, **kwargs: json.dumps([
        {"Name": "safe-local", "Endpoints": {"docker": {"Host": endpoint}}}
    ]))
    lab._check_local_daemon()
    assert lab._context_name == "safe-local"


def test_docker_host_override_is_refused_before_contact(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setattr(lab, "_run", lambda *_: pytest.fail("Docker must not be contacted"))
    with pytest.raises(lab_module.LabError, match="DOCKER_HOST overrides"):
        lab._check_local_daemon()


def test_valid_manifest_is_accepted(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab, "_compose", lambda *_: json.dumps(manifest_for(lab)))
    lab._validate_manifest()


@pytest.mark.parametrize("violation", [
    "public_port", "static_port", "shared_network", "external_volume", "remote_volume",
    "socket_mount", "extra_service", "latest_image", "extra_host", "foreign_secret",
])
def test_manifest_rejects_boundary_expansion(lab: Any, monkeypatch: pytest.MonkeyPatch, violation: str) -> None:
    manifest = copy.deepcopy(manifest_for(lab))
    if violation == "public_port":
        manifest["services"]["api"]["ports"] = [{"host_ip": "0.0.0.0", "target": 8000}]
    elif violation == "static_port":
        manifest["services"]["api"]["ports"] = [{"host_ip": "127.0.0.1", "target": 8000, "published": "8000"}]
    elif violation == "shared_network":
        manifest["networks"]["lab"]["internal"] = False
    elif violation == "external_volume":
        manifest["volumes"]["mysql_data"]["external"] = True
    elif violation == "remote_volume":
        manifest["volumes"]["mysql_data"]["driver_opts"] = {"type": "nfs", "device": ":/production"}
    elif violation == "socket_mount":
        manifest["services"]["api"]["volumes"] = [{"type": "bind", "source": "/var/run/docker.sock",
                                                   "target": "/var/run/docker.sock", "read_only": True}]
    elif violation == "extra_service":
        manifest["services"]["production"] = {}
    elif violation == "latest_image":
        manifest["services"]["mysql"]["image"] = "mysql:latest"
    elif violation == "extra_host":
        manifest["services"]["api"]["extra_hosts"] = {"mysql": "10.0.0.1"}
    else:
        manifest["secrets"]["mysql_probe_password"]["file"] = str(lab_module.EXAMPLE_DIR / "existing-secret")
    monkeypatch.setattr(lab, "_compose", lambda *_: json.dumps(manifest))
    with pytest.raises(lab_module.LabError):
        lab._validate_manifest()


def test_unknown_fault_rejected_without_docker(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab, "_run", lambda *_: pytest.fail("Docker must not be contacted"))
    with pytest.raises(lab_module.LabError, match="Unknown fault"):
        lab.fault("delete-all")


def test_only_scoped_down_with_volumes_is_used(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    lab.state["owned"] = True
    lab._save_state()
    calls = []
    monkeypatch.setattr(lab, "_prepare_mutation", lambda: None)
    monkeypatch.setattr(lab, "_mutate", lambda arguments: calls.append(arguments))
    monkeypatch.setattr(lab, "_resources", lambda: {"containers": [], "networks": [], "volumes": []})
    lab.close()
    assert calls == [["down", "--volumes", "--timeout", "10"]]
    assert lab.state["closed"] is True
    lab.close()
    assert len(calls) == 1


def test_docker_error_redacts_runtime_credentials(lab: Any) -> None:
    message = "server rejected " + " ".join(lab._passwords)
    safe = lab._safe_error(message)
    assert safe.count("[REDACTED]") == 3
    assert not any(secret in safe for secret in lab._passwords)


def test_path_link_and_junction_guards(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = lab_module.EXAMPLE_DIR / ".runtime"
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == runtime or original(path))
    with pytest.raises(lab_module.LabError, match="junctions"):
        lab_module.create_lab()
    with pytest.raises(lab_module.LabError, match="junctions"):
        lab_module.ComposeLab(lab.config)


@pytest.mark.parametrize("violation", ["rootfs_write", "extra_secret", "tmpfs", "startup_command"])
def test_probe_cannot_expand_its_execution_or_mounts(lab: Any, monkeypatch: pytest.MonkeyPatch, violation: str) -> None:
    manifest = manifest_for(lab)
    probe = manifest["services"]["probe"]
    if violation == "rootfs_write":
        probe["read_only"] = False
    elif violation == "extra_secret":
        probe["secrets"].append({"source": "mysql_root_password"})
    elif violation == "tmpfs":
        probe["tmpfs"] = ["/tmp"]
    else:
        probe["command"] = ["sh"]
    monkeypatch.setattr(lab, "_compose", lambda *_: json.dumps(manifest))
    with pytest.raises(lab_module.LabError):
        lab._validate_manifest()


def test_readiness_uses_only_fixed_nonroot_probe_command(lab: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(lab, "_probe_container", lambda: "a" * 64)
    def run(arguments: list[str], timeout: int) -> str:
        calls.append((arguments, timeout))
        return json.dumps({"status_code": 200, "body": {"dependencies": {"mysql": "ok", "redis": "ok"}}})
    monkeypatch.setattr(lab, "_run", run)
    assert lab._ready_request()["status_code"] == 200
    assert calls == [(["exec", "--user", "10001:10001", "a" * 64,
                      "python", "/app/probe.py", "ready"], 10)]
