"""Fresh, labelled enterprise deployments; secrets never enter argv or stdout."""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed


ROOT = Path(__file__).resolve().parent
LABEL = "io.holmes.enterprise"
SERVICES = {"db", "queue", "migrate", "api", "worker", "gateway", "prometheus"}
NETWORKS = {"core", "egress", "edge"}
VOLUMES = {"postgres_data", "queue_data", "prometheus_data"}
PINNED_IMAGES = {
    "db": "postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826",
    "queue": "valkey/valkey:8.1.10-alpine@sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507",
    "gateway": "nginx:1.30.5-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94",
    "prometheus": "prom/prometheus:v3.13.4@sha256:87861b8cf91579109319ebc300f3f1060e6da9c05d6ae8ad15a20c879e84e32e",
}


class DeploymentError(RuntimeError):
    pass


def checked_path(path: Path, root: Path | None = None) -> Path:
    root = root or ROOT
    path = path.absolute()
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path must stay inside this enterprise module")
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("Linked paths and junctions are forbidden")
        if candidate == root:
            break
    return path


def private(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    user = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                          text=True, check=True, timeout=10)
    match = re.search(r"S-1-5-[0-9-]+", user.stdout)
    if not match:
        raise ValueError("Cannot verify the local account")
    rights = "(OI)(CI)F" if path.is_dir() else "F"
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{match.group()}:{rights}",
                    "*S-1-5-18:" + rights], capture_output=True, check=True, timeout=10)


def fresh_text(path: Path, value: str, container_secret: bool = False) -> None:
    checked_path(path)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
    if os.name != "nt":
        # The secret parent is mode 0700; selected bind-secret files must also
        # be readable by PostgreSQL UID999 and application UID10001.
        path.chmod(0o444 if container_secret else 0o600)


def create(mode: str = "enterprise", image: str = "holmes-erp-enterprise:unbuilt", unavailable_fixture: bool = False) -> Path:
    if mode not in {"enterprise", "test"}:
        raise ValueError("Unknown deployment mode")
    if not re.fullmatch(r"holmes-erp-enterprise:[a-zA-Z0-9_.-]+", image):
        raise ValueError("An explicit local enterprise image is required")
    identity = secrets.token_hex(16)
    project = "holmes-enterprise-" + identity[:12]
    directory = checked_path(ROOT / ".runtime" / project)
    directory.mkdir(parents=True, exist_ok=False)
    private(directory)
    secret_dir, state_dir = directory / "secrets", directory / "state"
    secret_dir.mkdir()
    state_dir.mkdir()
    private(secret_dir)
    private(state_dir)
    (state_dir / "credentials").mkdir()
    (state_dir / "collectors").mkdir()
    root_password, app_password, migration_password, queue_password = (secrets.token_hex(32) for _ in range(4))
    values = {
        "postgres_root_password": root_password, "postgres_app_password": app_password,
        "postgres_migration_password": migration_password,
        "queue_password": queue_password, "metrics_token": secrets.token_hex(32),
        "database_url": f"postgresql+pg8000://workbench_app:{app_password}@db:5432/holmes",
        "migration_database_url": f"postgresql+pg8000://workbench_migrator:{migration_password}@db:5432/holmes",
        "broker_url": f"redis://default:{queue_password}@queue:6379/0",
        "queue_acl": ("user default on #" + hashlib.sha256(queue_password.encode()).hexdigest()
                      + " ~* &* +@all -config -shutdown -debug -flushall -flushdb -acl -module -migrate -replicaof -slaveof\n"),
        "postgres-init.sql": (f"CREATE ROLE workbench_migrator LOGIN PASSWORD '{migration_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
                              f"CREATE ROLE workbench_app LOGIN PASSWORD '{app_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
                              "CREATE DATABASE holmes OWNER workbench_migrator;\n"
                              "REVOKE ALL ON DATABASE holmes FROM PUBLIC;\n"
                              "GRANT CONNECT ON DATABASE holmes TO workbench_app;\n"
                              "\\connect holmes\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
                              "GRANT USAGE ON SCHEMA public TO workbench_app;\n"
                              + ("\\connect postgres\nCREATE DATABASE enterprise_store_test OWNER workbench_migrator;\n"
                                 "REVOKE ALL ON DATABASE enterprise_store_test FROM PUBLIC;\n"
                                 "GRANT CONNECT ON DATABASE enterprise_store_test TO workbench_app;\n"
                                 "\\connect enterprise_store_test\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
                                 "GRANT USAGE ON SCHEMA public TO workbench_app;\n" if mode == "test" else "")),
    }
    for name, value in values.items():
        fresh_text(secret_dir / name, value, container_secret=True)
    settings = {"allowed_erp_origins": ["https://erp-enterprise-fixture.invalid", "http://127.0.0.1:19090"] if unavailable_fixture else [],
                "workbench_origins": ["http://127.0.0.1:8875", "http://localhost:8875"],
                "queue_backend": "celery"}
    fresh_text(state_dir / "settings.json", json.dumps(settings, indent=2))
    fresh_text(state_dir / "initial-login.json", json.dumps({"username": "admin", "password": secrets.token_urlsafe(24)}))
    if unavailable_fixture:
        fresh_text(state_dir / "credentials/queue-fixture.json", json.dumps({"api_key": secrets.token_hex(16), "api_secret": secrets.token_hex(16)}))
    config = {"project": project, "identity": identity, "mode": mode, "image": image,
              "secret_dir": str(secret_dir), "state_dir": str(state_dir), "context": "desktop-linux"}
    fresh_text(directory / "config.json", json.dumps(config, indent=2))
    fresh_text(directory / "ownership.json", json.dumps({"identity": identity, "containers": [], "networks": [], "volumes": []}))
    return directory / "config.json"


class Enterprise:
    def __init__(self, config_path: Path):
        self.path = checked_path(config_path)
        self.directory = self.path.parent
        config = json.loads(self.path.read_text(encoding="utf-8"))
        if set(config) != {"project", "identity", "mode", "image", "secret_dir", "state_dir", "context"}:
            raise ValueError("Unexpected configuration fields")
        if not re.fullmatch(r"[a-f0-9]{32}", config["identity"]):
            raise ValueError("Invalid deployment identity")
        if config["project"] != "holmes-enterprise-" + config["identity"][:12] or self.directory.name != config["project"]:
            raise ValueError("Deployment path and identity differ")
        if self.directory.parent != ROOT / ".runtime" or config["mode"] not in {"enterprise", "test"}:
            raise ValueError("Only this module's fresh runtime can be used")
        if config["context"] != "desktop-linux" or os.environ.get("DOCKER_HOST"):
            raise ValueError("Remote Docker contexts and overrides are forbidden")
        for key, name in (("secret_dir", "secrets"), ("state_dir", "state")):
            if checked_path(Path(config[key])) != self.directory / name:
                raise ValueError("Runtime subdirectory is not owned by this project")
        if not re.fullmatch(r"holmes-erp-enterprise:[a-zA-Z0-9_.-]+", config["image"]):
            raise ValueError("Image is outside the enterprise scope")
        self.config = config
        self.owner_path = checked_path(self.directory / "ownership.json")
        self.owner = json.loads(self.owner_path.read_text())
        if self.owner.get("identity") != config["identity"]:
            raise ValueError("Ownership record differs")
        endpoint = json.loads(self.docker(["context", "inspect", config["context"]]))[0]["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith(("npipe:////./pipe/", "unix:///")):
            raise ValueError("Only an inspected local Docker endpoint is allowed")

    def docker(self, args, *, timeout=120, binary=False, input_file=None, output_file=None):
        result = subprocess.run(["docker", "--context", "desktop-linux", *args],
                                stdout=output_file if output_file else subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=input_file,
                                text=not binary, encoding=None if binary else "utf-8",
                                errors=None if binary else "replace", timeout=timeout, shell=False)
        if result.returncode:
            # A driver can include credentials/data in an error; never echo it.
            raise DeploymentError(f"Docker operation failed (exit {result.returncode})")
        return result.stdout

    def compose(self, args, *, timeout=180):
        config = self.config
        environment = dict(os.environ)
        environment.update({"ENTERPRISE_INSTANCE_ID": config["identity"], "ENTERPRISE_IMAGE": config["image"],
                            "ENTERPRISE_SECRET_DIR": Path(config["secret_dir"]).as_posix(),
                            "ENTERPRISE_STATE_DIR": Path(config["state_dir"]).as_posix()})
        argv = ["docker", "--context", "desktop-linux", "compose", "--project-name", config["project"],
                "--file", str(ROOT / "compose.yaml")]
        if config["mode"] == "test":
            argv.extend(["--file", str(ROOT / "compose.test.yaml")])
        result = subprocess.run([*argv, *args], env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, shell=False)
        if result.returncode:
            raise DeploymentError(f"Compose operation failed (exit {result.returncode})")
        return result.stdout

    def resources(self):
        project = self.config["project"]
        containers = self.docker(["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split()
        networks = self.docker(["network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]).split()
        volumes = self.docker(["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]).split()
        result = {"containers": json.loads(self.docker(["inspect", *containers])) if containers else [],
                  "networks": json.loads(self.docker(["network", "inspect", *networks])) if networks else [],
                  "volumes": json.loads(self.docker(["volume", "inspect", *volumes])) if volumes else []}
        return result

    def guard(self, resources, allow_new=False):
        project, identity = self.config["project"], self.config["identity"]
        ids = {item["Id"] for item in resources["containers"]}
        listed_volumes = {item["Name"] for item in resources["volumes"]}
        for kind, entries in resources.items():
            for item in entries:
                labels = item["Config"]["Labels"] if kind == "containers" else item["Labels"]
                key = item["Name"] if kind == "volumes" else item["Id"]
                if labels.get(LABEL) != identity or labels.get("com.docker.compose.project") != project:
                    raise ValueError("Resource labels are outside this deployment")
                if not allow_new and key not in self.owner[kind]:
                    raise ValueError("Existing resources cannot be adopted")
                if kind == "containers":
                    service = labels.get("com.docker.compose.service")
                    if service not in SERVICES or item["Name"].lstrip("/") != f"{project}-{service}-1":
                        raise ValueError("Unexpected container/service")
                    if item["HostConfig"]["Privileged"] or item["HostConfig"]["NetworkMode"] == "host":
                        raise ValueError("Privileged and host-network containers are forbidden")
                    if not item["HostConfig"]["ReadonlyRootfs"] or item["Config"]["User"].split(":")[0] in {"", "0", "root"}:
                        raise ValueError("A service must be nonroot and read-only")
                    if item["Config"]["Image"] != PINNED_IMAGES.get(service, self.config["image"]):
                        raise ValueError("A service is not using its reviewed image")
                    if item["HostConfig"]["CapDrop"] != ["ALL"] or "no-new-privileges:true" not in item["HostConfig"]["SecurityOpt"]:
                        raise ValueError("Container process restrictions changed")
                    names = set(item["NetworkSettings"]["Networks"])
                    expected_networks = {"core", "egress"} if service in {"api", "worker"} else {"core", "edge"} if service == "gateway" else {"core"}
                    if names != {f"{project}_{name}" for name in expected_networks}:
                        raise ValueError("A service joined an external/shared network")
                    self.guard_mounts(service, item["Mounts"])
                    if any(mount["Type"] == "volume" and mount["Name"] not in listed_volumes for mount in item["Mounts"]):
                        raise ValueError("A mounted volume has no verified ownership labels")
                    for port, bindings in item["NetworkSettings"]["Ports"].items():
                        if not bindings:
                            continue
                        expected_port = "8080/tcp" if service == "gateway" else "5432/tcp" if service == "db" else "6379/tcp" if service == "queue" else None
                        allowed = service == "gateway" or self.config["mode"] == "test" and service in {"db", "queue"}
                        if not allowed or port != expected_port or len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                            raise ValueError("An unreviewed host port was published")
                        if service == "gateway" and bindings[0]["HostPort"] != "8875":
                            raise ValueError("Gateway must use the isolated enterprise port")
                elif kind == "networks":
                    name = labels.get("com.docker.compose.network")
                    if name not in NETWORKS or item["Name"] != f"{project}_{name}":
                        raise ValueError("Unexpected network")
                    if name == "core" and item["Internal"] != (self.config["mode"] == "enterprise"):
                        raise ValueError("Core network isolation differs from the selected mode")
                    if not set(item.get("Containers", {})).issubset(ids):
                        raise ValueError("Network contains a foreign member")
                elif labels.get("com.docker.compose.volume") not in VOLUMES:
                    raise ValueError("Unexpected persistent volume")

    def guard_mounts(self, service, mounts):
        secrets_by_service = {"db": {"postgres_root_password", "postgres_app_password", "postgres_migration_password"},
                              "queue": {"queue_acl", "queue_password"}, "api": {"database_url", "broker_url", "metrics_token"},
                              "worker": {"database_url", "broker_url", "metrics_token"},
                              "migrate": {"database_url", "migration_database_url", "broker_url", "metrics_token"},
                              "prometheus": {"metrics_token"}, "gateway": set()}
        bindings = {"/run/secrets/" + name: (Path(self.config["secret_dir"]) / name, False)
                    for name in secrets_by_service[service]}
        if service in {"api", "worker", "migrate"}:
            bindings["/state"] = (Path(self.config["state_dir"]), True)
            bindings.update({"/state/" + name: (Path(self.config["state_dir"]) / name, False)
                             for name in ("settings.json", "initial-login.json", "credentials", "collectors")})
        static = {"db": {"/docker-entrypoint-initdb.d/10-holmes.sql": Path(self.config["secret_dir"]) / "postgres-init.sql",
                         "/opt/enterprise/db-export.sh": ROOT / "scripts/db-export.sh", "/opt/enterprise/db-restore.sh": ROOT / "scripts/db-restore.sh"},
                  "queue": {"/etc/valkey/enterprise.conf": ROOT / "config/valkey.conf"},
                  "gateway": {"/etc/nginx/enterprise.conf": ROOT / "config/nginx.conf"},
                  "prometheus": {"/etc/prometheus/enterprise.yaml": ROOT / "config/prometheus.yaml"}}
        bindings.update({destination: (source, False) for destination, source in static.get(service, {}).items()})
        volumes = {"db": ("/var/lib/postgresql/data", "postgres_data"), "queue": ("/data", "queue_data"),
                   "prometheus": ("/prometheus", "prometheus_data")}
        expected_destinations = set(bindings)
        if service in volumes:
            expected_destinations.add(volumes[service][0])
        if len(mounts) != len(expected_destinations) or {mount["Destination"] for mount in mounts} != expected_destinations:
            raise ValueError("A required mount is missing or duplicated")
        for mount in mounts:
            destination = mount["Destination"]
            if mount["Type"] == "volume":
                if service not in volumes or (destination, mount["Name"]) != (volumes[service][0], self.config["project"] + "_" + volumes[service][1]):
                    raise ValueError("A foreign persistent volume is mounted")
            elif mount["Type"] == "bind":
                if destination not in bindings:
                    raise ValueError("A foreign host path is mounted")
                source, writable = bindings[destination]
                expected = source.as_posix().lower()
                actual = mount["Source"].replace("\\", "/").lower()
                allowed_sources = {expected}
                if re.match(r"^[a-z]:/", expected):
                    suffix = "/" + expected[0] + expected[2:]
                    allowed_sources.update({"/run/desktop/mnt/host" + suffix, "/host_mnt" + suffix})
                if actual not in allowed_sources:
                    raise ValueError("A mount source differs from this private runtime")
                if mount["RW"] != writable:
                    raise ValueError("A mount permission differs from the reviewed manifest")
            else:
                raise ValueError("Unexpected mount type")

    def record(self, resources):
        self.guard(resources, allow_new=True)
        values = {"identity": self.config["identity"]}
        for kind, entries in resources.items():
            values[kind] = [item["Name"] if kind == "volumes" else item["Id"] for item in entries]
        self.owner_path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        self.owner = values

    def up(self, monitoring=False, data_only=False, refresh_gateway=False):
        self.guard(self.resources())
        self.guard_collisions()
        self.preflight()
        if self.config["mode"] == "enterprise" and not data_only:
            self.docker(["image", "inspect", self.config["image"]])
        args = (["--profile", "monitoring"] if monitoring else []) + ["up", "--detach"]
        if self.config["mode"] == "test" or data_only:
            args.extend(["db", "queue"])
        try:
            self.compose(args, timeout=600)
        finally:
            self.record(self.resources())
        for attempt in Retrying(stop=stop_after_delay(180), wait=wait_fixed(3),
                                retry=retry_if_exception_type(DeploymentError), reraise=True):
            with attempt:
                resources = self.resources()
                self.guard(resources)
                expected = {"db", "queue"} if self.config["mode"] == "test" or data_only else {"db", "queue", "api", "worker", "gateway"}
                healthy = {item["Config"]["Labels"]["com.docker.compose.service"] for item in resources["containers"]
                           if item["State"].get("Health", {}).get("Status") == "healthy"}
                if refresh_gateway and {"api", "worker"}.issubset(healthy):
                    gateway = next(item for item in resources["containers"] if item["Config"]["Labels"]["com.docker.compose.service"] == "gateway")
                    self.docker(["exec", "--user", "101:101", gateway["Id"], "nginx", "-c", "/etc/nginx/enterprise.conf", "-s", "reload"])
                    refresh_gateway = False
                if not expected.issubset(healthy):
                    raise DeploymentError("Owned services have not all become healthy")
        if self.config["mode"] == "test":
            self.host_credentials()
        return self.status()

    def active_tasks(self, resources):
        db = next(item for item in resources["containers"] if item["Config"]["Labels"]["com.docker.compose.service"] == "db")
        query = "SELECT count(*) FROM records WHERE kind='task' AND payload->>'state' IN ('queued','running')"
        command = ('PGPASSWORD="$(cat /run/secrets/postgres_app_password)"; export PGPASSWORD; '
                   'psql --host=127.0.0.1 --username=workbench_app --dbname=holmes --no-psqlrc '
                   '--set=ON_ERROR_STOP=1 -At -c "' + query + '"')
        count = self.docker(["exec", "--user", "999:999", db["Id"], "sh", "-c", command]).strip()
        if not re.fullmatch(r"[0-9]+", count):
            raise DeploymentError("Could not verify durable task inactivity")
        return int(count)

    def upgrade(self, image):
        if not re.fullmatch(r"holmes-erp-enterprise:[a-zA-Z0-9_.-]+", image) or image == self.config["image"] or image.lower().endswith(":latest"):
            raise ValueError("A new explicit local immutable image tag is required")
        resources = self.resources()
        self.guard(resources)
        self.guard_collisions()
        self.preflight()
        info = json.loads(self.docker(["image", "inspect", image]))[0]
        if info["Config"].get("User") != "10001:10001":
            raise ValueError("Upgrade image must use the reviewed application UID")
        services = {item["Config"]["Labels"]["com.docker.compose.service"]: item for item in resources["containers"]}
        if not {"db", "queue", "api", "worker", "migrate", "gateway"}.issubset(services):
            raise ValueError("Upgrade requires the complete owned enterprise deployment")
        if self.active_tasks(resources):
            raise ValueError("Queued/running tasks must finish before an upgrade")
        # Quiesce new admission, then check again before touching the worker.
        self.docker(["stop", "--time", "30", services["api"]["Id"]])
        try:
            resources = self.resources()
            self.guard(resources)
            if self.active_tasks(resources):
                raise ValueError("A task arrived before admission stopped; preserve it and retry later")
            backup = self.backup()
            path = checked_path(self.directory / ("config-version-" + secrets.token_hex(6) + ".json"))
            config = dict(self.config, image=image)
            fresh_text(path, json.dumps(config, indent=2))
        except Exception:
            self.guard(self.resources())
            self.docker(["start", services["api"]["Id"]])
            raise
        # IDs were verified before admission stopped and again before removal.
        self.guard(self.resources())
        self.docker(["rm", "--force", *[services[name]["Id"] for name in ("worker", "api", "migrate")]])
        updated = Enterprise(path)
        status = updated.up(monitoring="prometheus" in services, refresh_gateway=True)
        return {"config": str(path), "previous_config_preserved": str(self.path), "backup": str(backup),
                "database_queue_volumes_preserved": True, "image": image, "status": status}

    def guard_collisions(self):
        """A missing project label must never hide an existing same-name object."""
        project = self.config["project"]
        expected = {"containers": {f"{project}-{service}-1" for service in SERVICES},
                    "networks": {f"{project}_{name}" for name in NETWORKS},
                    "volumes": {f"{project}_{name}" for name in VOLUMES}}
        commands = {"containers": ["ps", "-a", "--format", "{{.Names}}"],
                    "networks": ["network", "ls", "--format", "{{.Name}}"],
                    "volumes": ["volume", "ls", "--format", "{{.Name}}"]}
        result = {"containers": [], "networks": [], "volumes": []}
        for kind, args in commands.items():
            names = set(self.docker(args).splitlines()) & expected[kind]
            if names:
                inspect = ["inspect"] if kind == "containers" else ["network", "inspect"] if kind == "networks" else ["volume", "inspect"]
                result[kind] = json.loads(self.docker([*inspect, *sorted(names)]))
        self.guard(result)

    def preflight(self):
        manifest = json.loads(self.compose(["--profile", "monitoring", "config", "--format", "json"]))
        if set(manifest["services"]) != SERVICES:
            raise ValueError("Compose must contain only the reviewed enterprise services")
        allowed_fields = {"image", "user", "environment", "secrets", "volumes", "tmpfs", "networks", "labels",
                          "logging", "healthcheck", "mem_limit", "cpus", "read_only", "cap_drop", "security_opt",
                          "restart", "command", "entrypoint", "depends_on", "ports", "profiles"}
        commands = {
            "db": None, "queue": ["valkey-server", "/etc/valkey/enterprise.conf"],
            "migrate": ["python", "-m", "holmes.erpnext_support", "migrate", "--settings", "/state/settings.json"],
            "api": ["python", "-m", "holmes.erpnext_support", "serve", "--settings", "/state/settings.json", "--host", "0.0.0.0", "--port", "8765"],
            "worker": ["python", "-m", "celery", "-A", "holmes.erpnext_support.worker:celery_app", "worker", "--pool=prefork", "--concurrency=1", "--loglevel=WARNING"],
            "gateway": None,
            "prometheus": ["--config.file=/etc/prometheus/enterprise.yaml", "--storage.tsdb.path=/prometheus", "--storage.tsdb.retention.time=7d", "--storage.tsdb.retention.size=256MB"],
        }
        for service, value in manifest["services"].items():
            if set(value) - allowed_fields:
                raise ValueError("Compose contains an unreviewed service option")
            expected_entrypoint = ["nginx", "-c", "/etc/nginx/enterprise.conf", "-g", "daemon off;"] if service == "gateway" else None
            if value.get("command") != commands[service] or value.get("entrypoint") != expected_entrypoint:
                raise ValueError("Compose command/entrypoint differs from the reviewed service")
            if service == "migrate" and value.get("healthcheck") != {"disable": True}:
                raise ValueError("The one-shot migrator must disable the API image healthcheck")
            expected_user = "999:999" if service in {"db", "queue"} else "101:101" if service == "gateway" else "65534:65534" if service == "prometheus" else "10001:10001"
            if value.get("image") != PINNED_IMAGES.get(service, self.config["image"]) or value.get("user") != expected_user:
                raise ValueError("Compose image/user differs from the reviewed service")
            if value.get("read_only") is not True or value.get("cap_drop") != ["ALL"] or value.get("security_opt") != ["no-new-privileges:true"]:
                raise ValueError("Compose process restrictions changed")
            if value.get("labels", {}).get(LABEL) != self.config["identity"]:
                raise ValueError("Compose identity label differs")
            expected_networks = {"core", "egress"} if service in {"api", "worker"} else {"core", "edge"} if service == "gateway" else {"core"}
            if set(value["networks"]) != expected_networks:
                raise ValueError("Compose service networks differ")
            mounts = [{"Type": mount["type"], "Source": mount["source"], "Destination": mount["target"],
                       "Name": self.config["project"] + "_" + mount["source"], "RW": not mount.get("read_only", False)}
                      for mount in value.get("volumes", [])]
            for secret in value.get("secrets", []):
                if secret.get("target", secret["source"]) not in {secret["source"], "/run/secrets/" + secret["source"]}:
                    raise ValueError("A Compose secret was redirected")
                mounts.append({"Type": "bind", "Source": manifest["secrets"][secret["source"]]["file"],
                               "Destination": "/run/secrets/" + secret["source"], "RW": False})
            self.guard_mounts(service, mounts)
            ports = value.get("ports", [])
            expected_port = 8080 if service == "gateway" else 5432 if service == "db" else 6379 if service == "queue" else None
            allowed_port = service == "gateway" or self.config["mode"] == "test" and service in {"db", "queue"}
            if ports and (not allowed_port or len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or ports[0]["target"] != expected_port):
                raise ValueError("Compose contains an unreviewed public port")
            if ports and ports[0].get("published") != ("8875" if service == "gateway" else "0"):
                raise ValueError("Compose public port differs")
        if set(manifest["networks"]) != NETWORKS or set(manifest["volumes"]) != VOLUMES:
            raise ValueError("Compose contains a shared/unreviewed network or volume")
        for name, value in manifest["networks"].items():
            if value.get("external") or value.get("name") != self.config["project"] + "_" + name or value.get("labels", {}).get(LABEL) != self.config["identity"]:
                raise ValueError("Compose network is not owned")
            if value.get("driver", "bridge") != "bridge" or value.get("driver_opts"):
                raise ValueError("Only isolated local bridge networks are supported")
        if manifest["networks"]["core"].get("internal", False) != (self.config["mode"] == "enterprise"):
            raise ValueError("Compose core isolation differs")
        for name, value in manifest["volumes"].items():
            if value.get("external") or value.get("name") != self.config["project"] + "_" + name or value.get("labels", {}).get(LABEL) != self.config["identity"]:
                raise ValueError("Compose volume is not owned")
            if value.get("driver", "local") != "local" or value.get("driver_opts"):
                raise ValueError("Remote or redirected persistent storage is forbidden")
        expected_secrets = {"postgres_root_password", "postgres_app_password", "postgres_migration_password",
                            "database_url", "migration_database_url", "broker_url", "queue_acl", "queue_password", "metrics_token"}
        if set(manifest["secrets"]) != expected_secrets:
            raise ValueError("Compose secret set differs from the reviewed manifest")
        for name, value in manifest["secrets"].items():
            if set(value) - {"file", "name"} or checked_path(Path(value["file"])) != Path(self.config["secret_dir"]) / name:
                raise ValueError("Compose secret source is outside this private project")
        return {"compose_policy": "passed"}

    def host_credentials(self):
        resources = self.resources()
        self.guard(resources)
        ports = {}
        for service, port in (("db", "5432/tcp"), ("queue", "6379/tcp")):
            item = next(entry for entry in resources["containers"] if entry["Config"]["Labels"]["com.docker.compose.service"] == service)
            bindings = item["NetworkSettings"]["Ports"][port]
            if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                raise ValueError("Test endpoints must have one loopback binding")
            ports[service] = int(bindings[0]["HostPort"])
        directory = Path(self.config["secret_dir"])
        credentials = {
            "host_database_url": f"postgresql+pg8000://workbench_app:{(directory/'postgres_app_password').read_text()}@127.0.0.1:{ports['db']}/holmes",
            "host_migration_database_url": f"postgresql+pg8000://workbench_migrator:{(directory/'postgres_migration_password').read_text()}@127.0.0.1:{ports['db']}/holmes",
            "host_store_test_database_url": f"postgresql+pg8000://workbench_app:{(directory/'postgres_app_password').read_text()}@127.0.0.1:{ports['db']}/enterprise_store_test",
            "host_store_test_migration_database_url": f"postgresql+pg8000://workbench_migrator:{(directory/'postgres_migration_password').read_text()}@127.0.0.1:{ports['db']}/enterprise_store_test",
            "host_broker_url": f"redis://default:{(directory/'queue_password').read_text()}@127.0.0.1:{ports['queue']}/0",
        }
        for name, value in credentials.items():
            path = checked_path(directory / name)
            if path.exists():
                if path.read_text() != value:
                    raise ValueError("Existing host credential file cannot be replaced")
            else:
                fresh_text(path, value)
        return ports

    def status(self):
        resources = self.resources()
        self.guard(resources)
        return {"project": self.config["project"], "mode": self.config["mode"], "config": str(self.path),
                "services": [{"service": item["Config"]["Labels"]["com.docker.compose.service"],
                              "status": item["State"]["Status"], "health": item["State"].get("Health", {}).get("Status")}
                             for item in resources["containers"]]}

    def stop(self):
        self.guard(self.resources())
        self.compose(["stop"], timeout=180)

    def close(self):
        self.guard(self.resources())
        self.guard_collisions()
        self.compose(["--profile", "monitoring", "down", "--volumes"], timeout=180)
        self.record(self.resources())

    def backup(self):
        resources = self.resources()
        self.guard(resources)
        item = next(entry for entry in resources["containers"] if entry["Config"]["Labels"]["com.docker.compose.service"] == "db")
        directory = checked_path(self.directory / "backups")
        directory.mkdir(exist_ok=True)
        private(directory)
        path = directory / ("workbench-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4) + ".dump")
        with path.open("xb") as stream:
            self.docker(["exec", "--user", "999:999", item["Id"], "sh", "/opt/enterprise/db-export.sh"],
                        binary=True, output_file=stream)
        private(path)
        manifest = {"kind": "owned_postgresql_backup", "project": self.config["project"], "file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "contains_erp_database": False,
                    "contains_sensitive_business_evidence": True, "contains_session_and_account_state": True}
        fresh_text(path.with_suffix(".json"), json.dumps(manifest, indent=2))
        return path

    def restore(self, backup: Path):
        backup = checked_path(backup)
        if not backup.is_file() or backup.suffix != ".dump":
            raise ValueError("Restore requires an owned enterprise backup file")
        metadata = json.loads(backup.with_suffix(".json").read_text())
        if metadata.get("kind") != "owned_postgresql_backup" or metadata.get("sha256") != hashlib.sha256(backup.read_bytes()).hexdigest():
            raise ValueError("Backup identity/integrity verification failed")
        resources = self.resources()
        self.guard(resources)
        if any(item["Config"]["Labels"]["com.docker.compose.service"] not in {"db", "queue"} for item in resources["containers"]):
            raise ValueError("Restore requires a fresh project with only DB/queue prepared")
        if (self.directory / "restore-result.json").exists():
            raise ValueError("This target was already restored; choose a fresh project")
        item = next(entry for entry in resources["containers"] if entry["Config"]["Labels"]["com.docker.compose.service"] == "db")
        # The migrator may restore only into this fresh empty owned database.
        check = self.docker(["exec", "--user", "999:999", item["Id"], "sh", "-c",
                             'PGPASSWORD="$(cat /run/secrets/postgres_migration_password)"; export PGPASSWORD; '
                             'psql --host=127.0.0.1 --username=workbench_migrator --dbname=holmes --no-psqlrc -At '
                             '-c "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN (\'pg_catalog\',\'information_schema\')"'])
        if check.strip() != "0":
            raise ValueError("Refusing to restore over an existing schema/data")
        with backup.open("rb") as stream:
            self.docker(["exec", "-i", "--user", "999:999", item["Id"], "sh", "/opt/enterprise/db-restore.sh"],
                        binary=True, input_file=stream)
        # Restored browser tokens must not become valid on a fresh deployment.
        # This is a fixed maintenance query on the empty target just restored;
        # the source database and account password hashes are left untouched.
        self.docker(["exec", "--user", "999:999", item["Id"], "sh", "-c",
                     'PGPASSWORD="$(cat /run/secrets/postgres_migration_password)"; export PGPASSWORD; '
                     'psql --host=127.0.0.1 --username=workbench_migrator --dbname=holmes --no-psqlrc '
                     '--set=ON_ERROR_STOP=1 -c "BEGIN; DELETE FROM sessions; COMMIT;"'])
        fresh_text(self.directory / "restore-result.json", json.dumps({"source_sha256": metadata["sha256"],
                   "target_project": self.config["project"], "restored_into_empty_database": True,
                   "app_grants_require_migrate": True, "sessions_revoked": True,
                   "account_password_hashes_preserved": True}, indent=2))
        return {"restored_into_empty_database": True, "target_project": self.config["project"], "sessions_revoked": True}

    def create_migration_test_database(self):
        if self.config["mode"] != "test":
            raise ValueError("A migration fixture requires the isolated test project")
        resources = self.resources()
        self.guard(resources)
        item = next(entry for entry in resources["containers"] if entry["Config"]["Labels"]["com.docker.compose.service"] == "db")
        # Fixed name and SQL only. There is no arbitrary SQL/host/shell API.
        command = ['exec', '-i', '--user', '999:999', item['Id'], 'sh', '-c',
                   'PGPASSWORD="$(cat /run/secrets/postgres_root_password)"; export PGPASSWORD; '
                   'exec psql --host=127.0.0.1 --username=postgres --dbname=postgres --no-psqlrc --set=ON_ERROR_STOP=1']
        sql = ("CREATE DATABASE enterprise_migration_test OWNER workbench_migrator;\n"
               "REVOKE ALL ON DATABASE enterprise_migration_test FROM PUBLIC;\n"
               "GRANT CONNECT ON DATABASE enterprise_migration_test TO workbench_app;\n"
               "\\connect enterprise_migration_test\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
               "GRANT USAGE ON SCHEMA public TO workbench_app;\n")
        process = subprocess.run(["docker", "--context", "desktop-linux", *command], input=sql.encode(),
                                 capture_output=True, timeout=30, shell=False)
        if process.returncode:
            raise DeploymentError("Refused an existing or unavailable migration test database")
        directory = Path(self.config["secret_dir"])
        ports = self.host_credentials()
        for role, secret, filename in (("workbench_app", "postgres_app_password", "host_migration_test_database_url"),
                                       ("workbench_migrator", "postgres_migration_password", "host_migration_test_migration_database_url")):
            fresh_text(directory / filename, f"postgresql+pg8000://{role}:{(directory/secret).read_text()}@127.0.0.1:{ports['db']}/enterprise_migration_test")
        return {"database": "enterprise_migration_test", "credentials": "private URL files created"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "up", "check", "stop", "down", "backup", "restore", "upgrade", "migration-test-db"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mode", choices=("enterprise", "test"), default="enterprise")
    parser.add_argument("--image", default="holmes-erp-enterprise:unbuilt")
    parser.add_argument("--monitoring", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--unavailable-fixture", action="store_true")
    parser.add_argument("--data-only", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        print(json.dumps({"config": str(create(args.mode, args.image, args.unavailable_fixture))}))
        return
    if not args.config:
        parser.error("--config is required for an existing owned project")
    lab = Enterprise(args.config)
    if args.action == "up":
        print(json.dumps(lab.up(args.monitoring, args.data_only)))
    elif args.action == "check":
        print(json.dumps(lab.status()))
    elif args.action == "stop":
        lab.stop()
    elif args.action == "down":
        lab.close()
    elif args.action == "backup":
        print(json.dumps({"backup": str(lab.backup()), "contains_erp_database": False, "contains_sensitive_business_evidence": True}))
    elif args.action == "upgrade":
        print(json.dumps(lab.upgrade(args.image)))
    elif args.action == "migration-test-db":
        print(json.dumps(lab.create_migration_test_database()))
    elif args.action == "restore":
        if not args.backup:
            parser.error("restore requires --backup and a fresh empty target project")
        print(json.dumps(lab.restore(args.backup)))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Keep driver errors, DSNs and private JSON values out of CLI output.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
