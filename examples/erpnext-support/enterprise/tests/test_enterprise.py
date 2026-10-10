"""Hermetic deployment policy tests; no Docker, ERP or network operations."""

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml


SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("enterprise_policy_test", SOURCE / "enterprise.py")
enterprise = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enterprise)


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr(enterprise, "ROOT", tmp_path)
    monkeypatch.setattr(enterprise, "private", lambda path: None)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(enterprise.Enterprise, "docker", lambda self, args, **kwargs:
                        json.dumps([{"Endpoints": {"docker": {"Host": "npipe:////./pipe/dockerDesktopLinuxEngine"}}}]))
    path = enterprise.create(image="holmes-erp-enterprise:unit")
    return enterprise.Enterprise(path)


def manifest(lab):
    replacements = {"ENTERPRISE_INSTANCE_ID": lab.config["identity"], "ENTERPRISE_IMAGE": lab.config["image"],
                    "ENTERPRISE_SECRET_DIR": Path(lab.config["secret_dir"]).as_posix(),
                    "ENTERPRISE_STATE_DIR": Path(lab.config["state_dir"]).as_posix(), "ERP_SUPPORT_MODEL_API_KEY": ""}
    source = (SOURCE / "compose.yaml").read_text()
    source = re.sub(r"\$\{([A-Z_]+)[^}]*\}", lambda m: replacements[m[1]], source)
    value = yaml.safe_load(source)
    value = {key: value[key] for key in ("services", "networks", "volumes", "secrets")}
    for service in value["services"].values():
        service["networks"] = {name: {} for name in service["networks"]}
        volumes = []
        for volume in service.get("volumes", []):
            if isinstance(volume, str):
                source, target = volume.split(":")
                volume = {"type": "volume", "source": source, "target": target}
            else:
                volume = dict(volume)
                if volume["source"].startswith("./"):
                    volume["source"] = (enterprise.ROOT / volume["source"][2:]).as_posix()
            volumes.append(volume)
        service["volumes"] = volumes
        service["secrets"] = [{"source": s, "target": "/run/secrets/" + s} for s in service.get("secrets", [])]
    for key in ("networks", "volumes"):
        for name, resource in value[key].items():
            resource["name"] = lab.config["project"] + "_" + name
    return value


def test_fresh_private_credentials_and_no_customer_fixture(owned):
    directory = Path(owned.config["secret_dir"])
    assert len({(directory / name).read_text() for name in (
        "postgres_root_password", "postgres_app_password", "postgres_migration_password", "queue_password")}) == 4
    assert json.loads((Path(owned.config["state_dir"]) / "settings.json").read_text())["allowed_erp_origins"] == []
    sql = (directory / "postgres-init.sql").read_text()
    assert sql.count("NOSUPERUSER NOCREATEDB NOCREATEROLE") == 2
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in sql
    acl = (directory / "queue_acl").read_text()
    assert (directory / "queue_password").read_text() not in acl
    for command in ("config", "shutdown", "debug", "flushall", "flushdb", "acl", "module", "migrate", "replicaof", "slaveof"):
        assert "-" + command in acl


def test_fixture_is_explicit_and_alias_matches_driver(owned):
    config = enterprise.create(image="holmes-erp-enterprise:unit", unavailable_fixture=True)
    state = config.parent / "state"
    assert json.loads((state / "settings.json").read_text())["allowed_erp_origins"] == [
        "https://erp-enterprise-fixture.invalid", "http://127.0.0.1:19090"]
    assert set(json.loads((state / "credentials/queue-fixture.json").read_text())) == {"api_key", "api_secret"}


def test_no_existing_file_or_runtime_overwrite(owned, monkeypatch):
    settings = Path(owned.config["state_dir"]) / "settings.json"
    before = settings.read_bytes()
    with pytest.raises(FileExistsError):
        enterprise.fresh_text(settings, "overwritten")
    monkeypatch.setattr(enterprise.secrets, "token_hex", lambda size: owned.config["identity"])
    with pytest.raises(FileExistsError):
        enterprise.create(image="holmes-erp-enterprise:unit")
    assert settings.read_bytes() == before


def test_outside_paths_and_junctions_rejected(owned, monkeypatch):
    with pytest.raises(ValueError):
        enterprise.checked_path(enterprise.ROOT.parent / "outside")
    monkeypatch.setattr(Path, "is_junction", lambda path: path == enterprise.ROOT / ".runtime")
    with pytest.raises(ValueError, match="junction"):
        enterprise.checked_path(owned.path)


def test_remote_docker_override_rejected(owned, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    with pytest.raises(ValueError, match="Remote"):
        enterprise.Enterprise(owned.path)


def test_internal_metrics_gateway_is_not_public():
    value = yaml.safe_load((SOURCE / "config/prometheus.yaml").read_text())
    config = (SOURCE / "config/nginx.conf").read_text()
    assert value["scrape_configs"][0]["static_configs"][0]["targets"] == ["gateway:9091"]
    assert value["scrape_configs"][0]["follow_redirects"] is False
    assert "limit_except GET { deny all; }" in config
    assert "if ($request_method != GET) { return 405; }" in config
    assert "proxy_set_header Host localhost;" in config
    assert "proxy_set_header Authorization $http_authorization;" in config
    assert "location = /metrics { return 404; }" in config
    assert "9091" not in (SOURCE / "compose.yaml").read_text()


def test_reviewed_compose_preflight_passes(owned, monkeypatch):
    monkeypatch.setattr(owned, "compose", lambda args: json.dumps(manifest(owned)))
    assert owned.preflight() == {"compose_policy": "passed"}


@pytest.mark.parametrize("mutation", [
    lambda m: m["services"]["api"].update(privileged=True),
    lambda m: m["services"]["api"].update(devices=["/dev/kvm"]),
    lambda m: m["services"]["api"].update(pid="host"),
    lambda m: m["services"]["api"].update(user="0:0"),
    lambda m: m["services"]["api"].update(read_only=False),
    lambda m: m["services"]["api"].update(cap_drop=[]),
    lambda m: m["services"]["api"].update(command=["sh"]),
    lambda m: m["services"]["migrate"].pop("healthcheck"),
    lambda m: m["services"]["api"]["networks"].update(foreign={}),
    lambda m: m["services"]["api"]["volumes"].append({"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}),
    lambda m: m["services"]["api"]["volumes"][0].update(source="/foreign"),
    lambda m: m["services"]["api"]["volumes"][1].update(read_only=False),
    lambda m: m["services"]["api"]["volumes"].pop(1),
    lambda m: m["services"]["api"]["secrets"][0].update(target="/other"),
    lambda m: m["services"]["gateway"]["ports"][0].update(host_ip="0.0.0.0"),
    lambda m: m["services"]["gateway"]["ports"][0].update(published="8765"),
    lambda m: m["networks"]["core"].update(internal=False),
    lambda m: m["networks"]["core"].update(external=True),
    lambda m: m["networks"]["core"].update(driver_opts={"bridge": "foreign"}),
    lambda m: m["volumes"]["postgres_data"].update(driver_opts={"type": "nfs"}),
    lambda m: m["secrets"]["database_url"].update(file="/production/url"),
])
def test_tampered_compose_rejected_before_mutation(owned, monkeypatch, mutation):
    value = manifest(owned)
    mutation(value)
    calls = []
    monkeypatch.setattr(owned, "resources", lambda: {"containers": [], "networks": [], "volumes": []})
    monkeypatch.setattr(owned, "guard_collisions", lambda: None)
    monkeypatch.setattr(owned, "compose", lambda args: (calls.append(args), json.dumps(value))[1])
    with pytest.raises(ValueError):
        owned.up()
    assert calls == [["--profile", "monitoring", "config", "--format", "json"]]


@pytest.mark.parametrize("kind", ["networks", "volumes"])
def test_unlabeled_same_name_resource_rejected(owned, monkeypatch, kind):
    name = owned.config["project"] + ("_core" if kind == "networks" else "_postgres_data")
    def fake_docker(args, **kwargs):
        if args[:2] == ["network", "ls"] or args[:2] == ["volume", "ls"]:
            return name if args[0] == ("network" if kind == "networks" else "volume") else ""
        if "inspect" in args:
            return json.dumps([{"Id": "foreign", "Name": name, "Labels": {}}])
        return ""
    monkeypatch.setattr(owned, "docker", fake_docker)
    with pytest.raises(ValueError, match="labels"):
        owned.guard_collisions()


def test_existing_labeled_resource_cannot_be_adopted(owned):
    labels = {enterprise.LABEL: owned.config["identity"], "com.docker.compose.project": owned.config["project"]}
    with pytest.raises(ValueError, match="adopted"):
        owned.guard({"containers": [], "networks": [{"Id": "foreign", "Labels": labels}], "volumes": []})


def test_restore_clears_only_target_sessions_and_preserves_source(owned, monkeypatch):
    backup = enterprise.ROOT / "source.dump"
    backup.write_bytes(b"fixture-backup")
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    backup.with_suffix(".json").write_text(json.dumps({"kind": "owned_postgresql_backup", "sha256": digest}))
    resource = {"Id": "owned-db", "Config": {"Labels": {"com.docker.compose.service": "db"}}}
    monkeypatch.setattr(owned, "resources", lambda: {"containers": [resource]})
    monkeypatch.setattr(owned, "guard", lambda values: None)
    calls = []
    monkeypatch.setattr(owned, "docker", lambda args, **kwargs: (calls.append(args), "0")[1])
    result = owned.restore(backup)
    assert result["sessions_revoked"] is True
    assert "DELETE FROM sessions" in calls[-1][-1]
    assert all("owned-db" in call for call in calls)
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest
    assert json.loads((owned.directory / "restore-result.json").read_text())["account_password_hashes_preserved"]
    with pytest.raises(ValueError, match="already restored"):
        owned.restore(backup)


def test_restore_rejects_nonempty_database(owned, monkeypatch):
    backup = enterprise.ROOT / "source.dump"
    backup.write_bytes(b"fixture-backup")
    backup.with_suffix(".json").write_text(json.dumps({"kind": "owned_postgresql_backup", "sha256": hashlib.sha256(backup.read_bytes()).hexdigest()}))
    monkeypatch.setattr(owned, "resources", lambda: {"containers": [{"Id": "owned-db", "Config": {"Labels": {"com.docker.compose.service": "db"}}}]})
    monkeypatch.setattr(owned, "guard", lambda values: None)
    monkeypatch.setattr(owned, "docker", lambda args, **kwargs: "3")
    with pytest.raises(ValueError, match="existing schema"):
        owned.restore(backup)
    assert not (owned.directory / "restore-result.json").exists()


@pytest.fixture
def upgrade_setup(owned, monkeypatch):
    values = {"containers": [{"Id": "owned-" + name, "Config": {"Labels": {"com.docker.compose.service": name}}}
                             for name in ("db", "queue", "api", "worker", "migrate", "gateway")]}
    calls = []
    monkeypatch.setattr(owned, "resources", lambda: values)
    monkeypatch.setattr(owned, "guard", lambda resources: None)
    monkeypatch.setattr(owned, "guard_collisions", lambda: None)
    monkeypatch.setattr(owned, "preflight", lambda: None)
    monkeypatch.setattr(owned, "active_tasks", lambda resources: 0)
    monkeypatch.setattr(owned, "backup", lambda: owned.directory / "private-backup.dump")
    def fake_docker(args, **kwargs):
        calls.append(args)
        return json.dumps([{"Config": {"User": "10001:10001"}}]) if args[:2] == ["image", "inspect"] else ""
    monkeypatch.setattr(owned, "docker", fake_docker)
    return owned, calls


@pytest.mark.parametrize("image", ["foreign:tag", "holmes-erp-enterprise:latest", "holmes-erp-enterprise:unit"])
def test_upgrade_rejects_unreviewed_or_same_tag_before_operations(upgrade_setup, image):
    owned, calls = upgrade_setup
    with pytest.raises(ValueError):
        owned.upgrade(image)
    assert calls == []


def test_upgrade_never_interrupts_active_work(upgrade_setup, monkeypatch):
    owned, calls = upgrade_setup
    monkeypatch.setattr(owned, "active_tasks", lambda resources: 1)
    with pytest.raises(ValueError, match="finish"):
        owned.upgrade("holmes-erp-enterprise:new")
    assert all(args[0] not in {"stop", "rm"} for args in calls)
    assert not list(owned.directory.glob("config-version-*.json"))


def test_upgrade_admission_race_preserves_worker_and_restarts_api(upgrade_setup, monkeypatch):
    owned, calls = upgrade_setup
    counts = iter([0, 1])
    monkeypatch.setattr(owned, "active_tasks", lambda resources: next(counts))
    with pytest.raises(ValueError, match="arrived"):
        owned.upgrade("holmes-erp-enterprise:new")
    assert ["stop", "--time", "30", "owned-api"] in calls
    assert calls[-1] == ["start", "owned-api"]
    assert all(args[0] != "rm" for args in calls)


def test_upgrade_preserves_old_config_and_removes_only_verified_app_ids(upgrade_setup, monkeypatch):
    owned, calls = upgrade_setup
    previous = owned.path.read_bytes()
    class NewDeployment:
        def __init__(self, path):
            self.path = path
            self.config = json.loads(path.read_text())
        def up(self, **kwargs):
            assert kwargs == {"monitoring": False, "refresh_gateway": True}
            return {"services": []}
    monkeypatch.setattr(enterprise, "Enterprise", NewDeployment)
    result = owned.upgrade("holmes-erp-enterprise:new")
    assert owned.path.read_bytes() == previous
    assert Path(result["config"]).name.startswith("config-version-")
    assert json.loads(Path(result["config"]).read_text())["image"] == "holmes-erp-enterprise:new"
    assert ["rm", "--force", "owned-worker", "owned-api", "owned-migrate"] in calls
    assert all("owned-db" not in args and "owned-queue" not in args for args in calls)
    assert result["database_queue_volumes_preserved"] is True
