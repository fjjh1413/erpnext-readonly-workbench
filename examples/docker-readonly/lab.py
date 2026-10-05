"""New, opt-in fault fixture lifecycle. This module is not an Agent tool.

Only a fresh randomly named Compose project on a local Docker daemon may be
created. Later mutations require the saved resource IDs and both ownership
labels. The Agent itself receives the separate read-only toolset configuration.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

EXAMPLE_DIR = Path(__file__).resolve().parent
LAB_LABEL = "io.holmes.readonly-lab"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
SERVICES = {"api", "mysql", "redis", "probe"}
PASSWORD_FILES = (
    "mysql_root_password", "mysql_probe_password", "redis_probe_password"
)
FAULTS = (
    "database_unavailable", "cache_connection_error", "service_configuration_error"
)


class LabError(RuntimeError):
    """A lifecycle or ownership check failed; no wider Docker cleanup is allowed."""


class NotReady(LabError):
    pass


def _reject_linked_path(path: Path, anchor: Path) -> None:
    # Equivalent to the read-only toolset's guard; kept dependency-light so
    # lifecycle setup does not import Holmes' optional integrations.
    candidate, root = path.absolute(), anchor.absolute()
    if not candidate.is_relative_to(root):
        raise LabError("Generated runtime path is outside the example directory")
    for current in (candidate, *candidate.parents):
        if current.is_symlink() or current.is_junction():
            raise LabError("Generated runtime paths cannot contain symlinks or junctions")
        if current == root:
            break


def _write_new(path: Path, value: str) -> None:
    _reject_linked_path(path, EXAMPLE_DIR)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)
        return
    # chmod does not restrict Windows ACLs. Resolve the current SID without
    # using usernames, shell interpolation, or granting other local users.
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    match = re.search(r"S-1-5-[0-9-]+", identity.stdout)
    if not match:
        raise LabError("Cannot resolve the current Windows user SID")
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         f"*{match.group()}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True, text=True, check=True, timeout=10,
    )


def create_lab() -> dict[str, str]:
    """Create new local runtime files exclusively; never claim Docker resources."""
    runtime = EXAMPLE_DIR / ".runtime"
    _reject_linked_path(runtime, EXAMPLE_DIR)
    runtime.mkdir(exist_ok=True)
    lab_id = secrets.token_hex(16)
    project = f"holmes-lab-{lab_id[:12]}"
    directory = runtime / project
    directory.mkdir(mode=0o700, exist_ok=False)
    _restrict_directory(directory)
    secret_dir = directory / "secrets"
    secret_dir.mkdir(mode=0o700)
    for name in PASSWORD_FILES:
        secret = secret_dir / name
        _write_new(secret, secrets.token_hex(32))
        # The parent directory is private. Individual Compose bind-backed
        # secrets must remain readable by the non-root API process on Linux.
        secret.chmod(0o444)
    config = {"project": project, "lab_id": lab_id,
              "secret_dir": str(secret_dir.resolve())}
    _write_new(directory / "config.json", json.dumps(config, indent=2) + "\n")
    _write_new(directory / "compose.env", "")
    _write_new(directory / "ownership.json", json.dumps({
        "project": project, "lab_id": lab_id, "owned": False, "closed": False,
        "fault": None, "containers": [], "networks": [], "volumes": [],
    }, indent=2) + "\n")
    return config


class ComposeLab:
    def __init__(self, config: Mapping[str, Any]):
        if set(config) != {"project", "lab_id", "secret_dir"}:
            raise LabError("Lab config accepts only project, lab_id and secret_dir")
        self.config = dict(config)
        self.project = str(config["project"])
        self.lab_id = str(config["lab_id"])
        if not re.fullmatch(r"holmes-lab-[a-f0-9]{12}", self.project):
            raise LabError("Refusing a project outside the diagnostic lab namespace")
        if not re.fullmatch(r"[a-f0-9]{32}", self.lab_id):
            raise LabError("lab_id must contain 32 lowercase hexadecimal characters")
        self.directory = EXAMPLE_DIR / ".runtime" / self.project
        _reject_linked_path(self.directory, EXAMPLE_DIR)
        _reject_linked_path(Path(config["secret_dir"]), EXAMPLE_DIR)
        self.secret_dir = Path(config["secret_dir"]).resolve()
        if self.directory.is_symlink() or self.secret_dir != (self.directory / "secrets").resolve():
            raise LabError("Runtime files must belong to this example's project directory")
        if (EXAMPLE_DIR / ".runtime").is_symlink() or (self.directory / "secrets").is_symlink():
            raise LabError("Runtime paths must not contain symbolic links")
        self.config_path = self.directory / "config.json"
        _reject_linked_path(self.config_path, EXAMPLE_DIR)
        self.state_path = self.directory / "ownership.json"
        self.state = self._load_state()
        self._context_name: str | None = None
        self._passwords = []
        for name in PASSWORD_FILES:
            path = self.secret_dir / name
            _reject_linked_path(path, EXAMPLE_DIR)
            value = path.read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise LabError("Invalid generated credential file")
            self._passwords.append(value)

    def _load_state(self) -> dict[str, Any]:
        _reject_linked_path(self.state_path, EXAMPLE_DIR)
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LabError("Missing valid ownership record; existing resources cannot be adopted") from exc
        if state.get("project") != self.project or state.get("lab_id") != self.lab_id:
            raise LabError("The ownership record belongs to a different project")
        if state.get("fault") not in (None, *FAULTS):
            raise LabError("Unrecognized fault in the ownership record")
        if type(state.get("owned")) is not bool or type(state.get("closed")) is not bool:
            raise LabError("Invalid ownership state flags")
        for kind in ("containers", "networks", "volumes"):
            if not isinstance(state.get(kind), list) or not all(
                isinstance(value, str) for value in state[kind]
            ):
                raise LabError("Invalid resource ownership record")
        return state

    def _save_state(self) -> None:
        # Replace only our generated ownership record, never upstream files.
        temporary = self.state_path.with_suffix(".pending")
        _write_new(temporary, json.dumps(self.state, indent=2) + "\n")
        temporary.replace(self.state_path)

    def _safe_error(self, output: str) -> str:
        for password in self._passwords:
            output = output.replace(password, "[REDACTED]")
        return output[-4000:]

    def _env(self) -> dict[str, str]:
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith("COMPOSE_") or key.startswith("HOLMES_"):
                environment.pop(key)
        fault = self.state["fault"]
        environment.update({
            "HOLMES_PROJECT": self.project,
            "HOLMES_LAB_ID": self.lab_id,
            "HOLMES_SECRET_DIR": self.secret_dir.as_posix(),
            "HOLMES_API_REDIS_PORT": "6380" if fault == "cache_connection_error" else "6379",
            "HOLMES_API_APP_MODE": "invalid" if fault == "service_configuration_error" else "test-lab",
        })
        return environment

    def _run(self, arguments: list[str], timeout: int = 60) -> str:
        if os.environ.get("DOCKER_HOST"):
            raise LabError("DOCKER_HOST overrides are forbidden for the isolated lab")
        context = ["--context", self._context_name] if self._context_name else []
        if not context and arguments[:2] != ["context", "inspect"]:
            raise LabError("A verified local Docker context must be selected first")
        try:
            process = subprocess.run(
                ["docker", *context, *arguments], cwd=EXAMPLE_DIR, env=self._env(),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LabError(f"Local Docker command failed: {type(exc).__name__}") from exc
        if process.returncode:
            raise LabError(self._safe_error(process.stderr or process.stdout))
        return process.stdout

    def _compose(self, arguments: list[str], timeout: int = 60) -> str:
        return self._run([
            "compose", "--project-name", self.project,
            "--project-directory", str(EXAMPLE_DIR),
            "--file", str(EXAMPLE_DIR / "compose.yaml"),
            "--env-file", str(self.directory / "compose.env"), *arguments,
        ], timeout=timeout)

    def _check_local_daemon(self) -> None:
        if os.environ.get("DOCKER_HOST"):
            raise LabError("DOCKER_HOST overrides are forbidden for the isolated lab")
        contexts = json.loads(self._run(["context", "inspect"]))
        if not isinstance(contexts, list) or len(contexts) != 1:
            raise LabError("Expected exactly one local Docker context")
        endpoint = contexts[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
        name = contexts[0].get("Name", "")
        local = isinstance(endpoint, str) and (endpoint.startswith("unix:///") or bool(
            re.fullmatch(r"npipe:/{4}\.\/pipe/[A-Za-z0-9_.-]+", endpoint)
        ))
        if not local or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
            raise LabError("This fault lab requires a local Docker socket or named pipe")
        self._context_name = name

    def _validate_manifest(self) -> None:
        manifest = json.loads(self._compose(["config", "--format", "json"]))
        if set(manifest.get("services", {})) != SERVICES:
            raise LabError("Only api, mysql, redis and the fixed probe may exist in the lab manifest")
        networks = manifest.get("networks", {})
        if set(networks) != {"lab"} or networks["lab"].get("internal") is not True:
            raise LabError("The lab must use a single internal network")
        if networks["lab"].get("external") or networks["lab"].get("labels", {}).get(LAB_LABEL) != self.lab_id:
            raise LabError("External or unlabelled networks are forbidden")
        if networks["lab"].get("driver") not in (None, "bridge") or networks["lab"].get("driver_opts"):
            raise LabError("Only an ordinary local bridge network may be created")
        if networks["lab"].get("name") != f"{self.project}_lab":
            raise LabError("The network name must belong to this project")
        volumes = manifest.get("volumes", {})
        if set(volumes) != {"mysql_data"} or volumes["mysql_data"].get("external"):
            raise LabError("Shared or external data volumes are forbidden")
        if volumes["mysql_data"].get("driver") not in (None, "local") or volumes["mysql_data"].get("driver_opts"):
            raise LabError("Remote volume drivers are forbidden")
        if volumes["mysql_data"].get("name") != f"{self.project}_mysql_data":
            raise LabError("The data volume name must belong to this project")
        if volumes["mysql_data"].get("labels", {}).get(LAB_LABEL) != self.lab_id:
            raise LabError("The data volume must carry the lab ownership label")
        declared_secrets = manifest.get("secrets", {})
        if set(declared_secrets) != set(PASSWORD_FILES):
            raise LabError("Only generated runtime credentials may be mounted")
        for name, item in declared_secrets.items():
            if item.get("external") or Path(item.get("file", "")).resolve() != self.secret_dir / name:
                raise LabError("Secret mounts must point into this project's private runtime directory")
        expected_binds = {
            "api": set(),
            "probe": set(),
            "mysql": {
                (str((EXAMPLE_DIR / "mysql/init-probe.sh").resolve()), "/docker-entrypoint-initdb.d/10-probe.sh"),
                (str((EXAMPLE_DIR / "mysql/healthcheck.sh").resolve()), "/opt/lab/healthcheck.sh"),
            },
            "redis": {
                (str((EXAMPLE_DIR / "redis/start-redis.sh").resolve()), "/opt/lab/start-redis.sh"),
                (str((EXAMPLE_DIR / "redis/healthcheck.sh").resolve()), "/opt/lab/healthcheck.sh"),
            },
        }
        expected_images = {"api": f"{self.project}-api:lab", "probe": f"{self.project}-api:lab",
                           "mysql": "mysql:8.4.4", "redis": "redis:7.4.2-alpine"}
        for service, specification in manifest["services"].items():
            if specification.get("image") != expected_images[service]:
                raise LabError("The manifest must use the fixed lab images")
            if specification.get("labels", {}).get(LAB_LABEL) != self.lab_id:
                raise LabError("Every container must carry the lab ownership label")
            if set(specification.get("networks", {})) != {"lab"} or specification.get("network_mode"):
                raise LabError("Containers may join only the internal lab network")
            if specification.get("privileged") or specification.get("pid") or specification.get("ipc"):
                raise LabError("Privileged or shared host namespaces are forbidden")
            if any(specification.get(key) for key in (
                "extra_hosts", "external_links", "links", "dns", "devices", "volumes_from", "cap_add"
            )):
                raise LabError("Host, service and device overrides are forbidden")
            ports = specification.get("ports", [])
            if ports:
                raise LabError("Internal-only fixture services must not publish host ports")
            binds = set()
            data_mounts = []
            for mount in specification.get("volumes", []):
                if mount.get("type") == "bind":
                    if mount.get("read_only") is not True:
                        raise LabError("Source fixture mounts must be read-only")
                    binds.add((str(Path(mount["source"]).resolve()), mount["target"]))
                elif mount.get("type") == "volume":
                    data_mounts.append((mount["source"], mount["target"]))
                else:
                    raise LabError("Unexpected container mount")
            if binds != expected_binds[service]:
                raise LabError("Container bind mounts differ from the isolated fixture")
            expected_data = [("mysql_data", "/var/lib/mysql")] if service == "mysql" else []
            if data_mounts != expected_data:
                raise LabError("Only MySQL may mount this project's data volume")
            if service == "api":
                build = specification.get("build", {})
                if Path(build.get("context", "")).resolve() != (EXAMPLE_DIR / "app").resolve():
                    raise LabError("API build context must be the local fixture")
            if service == "probe":
                if specification.get("read_only") is not True or specification.get("tmpfs"):
                    raise LabError("The fixed probe must use a read-only root without extra mounts")
                probe_secrets = {item["source"] for item in specification.get("secrets", [])}
                if probe_secrets != {"mysql_probe_password", "redis_probe_password"}:
                    raise LabError("The fixed probe may mount only its two least-privileged credentials")
                if specification.get("command") != ["python", "-c", "import signal; signal.pause()"]:
                    raise LabError("Unexpected fixed probe startup command")

    def _resources(self) -> dict[str, list[str]]:
        filter_value = f"label={PROJECT_LABEL}={self.project}"
        return {
            "containers": self._run(["ps", "--all", "--quiet", "--no-trunc", "--filter", filter_value]).split(),
            "networks": self._run(["network", "ls", "--quiet", "--no-trunc", "--filter", filter_value]).split(),
            "volumes": self._run(["volume", "ls", "--quiet", "--filter", filter_value]).split(),
        }

    def _inspect(self, kind: str, identifiers: list[str]) -> list[dict[str, Any]]:
        if not identifiers:
            return []
        prefix = [] if kind == "containers" else ["network" if kind == "networks" else "volume"]
        return json.loads(self._run([*prefix, "inspect", *identifiers]))

    def _validate_resource(self, kind: str, resource: dict[str, Any]) -> None:
        labels = resource.get("Config", {}).get("Labels", {}) if kind == "containers" else resource.get("Labels", {})
        if not labels or labels.get(PROJECT_LABEL) != self.project or labels.get(LAB_LABEL) != self.lab_id:
            raise LabError("Refusing to mutate a resource without matching project and lab labels")
        if kind == "containers":
            service = labels.get(SERVICE_LABEL)
            if service not in SERVICES or resource.get("Name", "").lstrip("/") != f"{self.project}-{service}-1":
                raise LabError("An unexpected container exists in this project")
            networks = resource.get("NetworkSettings", {}).get("Networks", {})
            if set(networks) != {f"{self.project}_lab"}:
                raise LabError("A container is attached outside the lab network")
        elif kind == "networks":
            if resource.get("Internal") is not True or resource.get("Name") != f"{self.project}_lab":
                raise LabError("Refusing to mutate a non-internal or foreign network")
        elif resource.get("Name") != f"{self.project}_mysql_data":
            raise LabError("Refusing to mutate a foreign volume")

    def _assert_owned(self) -> None:
        resources = self._resources()
        if not self.state["owned"] and any(resources.values()):
            raise LabError("Existing Docker resources are never adopted, even if labelled as a lab")
        for kind, identifiers in resources.items():
            if not set(identifiers).issubset(set(self.state[kind])):
                raise LabError("A Docker resource was not created and recorded by this lifecycle")
            for resource in self._inspect(kind, identifiers):
                self._validate_resource(kind, resource)

    def _capture(self) -> None:
        resources = self._resources()
        for kind, identifiers in resources.items():
            for resource in self._inspect(kind, identifiers):
                self._validate_resource(kind, resource)
            self.state[kind] = identifiers
        self._save_state()

    def _prepare_mutation(self) -> None:
        self.state = self._load_state()
        if self.state["closed"]:
            raise LabError("This lab was closed; create a fresh runtime configuration")
        self._check_local_daemon()
        self._validate_manifest()
        self._assert_owned()

    def _mutate(self, arguments: list[str], timeout: int = 120) -> None:
        try:
            self._compose(arguments, timeout=timeout)
        finally:
            # A partially failed Compose operation can still create resources.
            # Record only scoped resources so the same lab can be cleaned up.
            self._capture()

    def up(self) -> None:
        self._prepare_mutation()
        self.state["owned"] = True
        self._save_state()
        self._mutate(["up", "--detach", "--build"], timeout=600)
        self.wait_ready()

    def _probe_container(self) -> str:
        self._assert_owned()
        for resource in self._inspect("containers", self.state["containers"]):
            if resource["Config"]["Labels"].get(SERVICE_LABEL) == "probe":
                if resource["State"]["Status"] != "running":
                    raise NotReady("The fixed internal probe is not running")
                if resource["Config"].get("Image") != f"{self.project}-api:lab" or resource["Config"].get("User") != "10001:10001":
                    raise LabError("The fixed internal probe must use the lab image and non-root user")
                if resource["HostConfig"].get("ReadonlyRootfs") is not True:
                    raise LabError("The fixed internal probe must have a read-only root filesystem")
                mounts = resource.get("Mounts", [])
                destinations = {item.get("Destination") for item in mounts}
                if destinations != {"/run/secrets/mysql_probe_password", "/run/secrets/redis_probe_password"} or len(mounts) != 2:
                    raise LabError("The fixed internal probe may mount only its two credentials")
                if any(item.get("RW") or item.get("Type") != "bind" for item in mounts):
                    raise LabError("The fixed internal probe credentials must be read-only file mounts")
                return resource["Id"]
        raise NotReady("The fixed internal probe is absent")

    def _ready_request(self) -> dict[str, Any]:
        identifier = self._probe_container()
        output = self._run(["exec", "--user", "10001:10001", identifier,
                            "python", "/app/probe.py", "ready"], timeout=10)
        if len(output.encode("utf-8")) > 4096:
            raise LabError("The fixed readiness response exceeded its size limit")
        response = json.loads(output)
        if not isinstance(response, dict) or not isinstance(response.get("status_code"), int) or not isinstance(response.get("body"), dict):
            raise LabError("The fixed readiness probe returned an unexpected schema")
        return response

    def wait_ready(self, timeout: int = 120) -> None:
        self._check_local_daemon()
        for attempt in Retrying(
            stop=stop_after_delay(timeout), wait=wait_fixed(1),
            retry=retry_if_exception_type(NotReady),
            reraise=True,
        ):
            with attempt:
                response = self._ready_request()
                if response["status_code"] != 200 or response["body"].get("dependencies") != {"mysql": "ok", "redis": "ok"}:
                    raise NotReady("The application's real dependency probes are not ready")

    def fault(self, name: str) -> None:
        if name not in FAULTS:
            raise LabError("Unknown fault; only the three isolated fixture faults are allowed")
        self._prepare_mutation()
        if not self.state["owned"] or not self.state["containers"]:
            raise LabError("Start a newly created lab before injecting a fault")
        if self.state["fault"] is not None:
            raise LabError("Restore the current fault before injecting another")
        self.state["fault"] = name
        self._save_state()
        if name == "database_unavailable":
            self._mutate(["stop", "--timeout", "10", "mysql"])
        else:
            self._mutate(["up", "--detach", "--no-deps", "--force-recreate", "api"])
        self._wait_fault()

    def _wait_fault(self) -> None:
        for attempt in Retrying(
            stop=stop_after_delay(60), wait=wait_fixed(1),
            retry=retry_if_exception_type(NotReady), reraise=True,
        ):
            with attempt:
                if self.state["fault"] == "service_configuration_error":
                    self._assert_owned()
                    containers = self._inspect("containers", self.state["containers"])
                    api = next(item for item in containers if item["Config"]["Labels"].get(SERVICE_LABEL) == "api")
                    if api.get("State", {}).get("Status") != "exited":
                        raise NotReady("Waiting for application configuration validation to fail")
                else:
                    if self._ready_request()["status_code"] != 503:
                        raise NotReady("Waiting for a failed real dependency readiness probe")

    def restore(self) -> None:
        self._prepare_mutation()
        if not self.state["owned"]:
            raise LabError("An unstarted lab cannot be restored")
        self.state["fault"] = None
        self._save_state()
        self._mutate(["up", "--detach", "mysql", "redis", "api", "probe"])
        self.wait_ready()

    def status(self) -> dict[str, Any]:
        self.state = self._load_state()
        self._check_local_daemon()
        self._assert_owned()
        containers = self._inspect("containers", self.state["containers"])
        return {"project": self.project, "lab_id": self.lab_id, "fault": self.state["fault"],
                "closed": self.state["closed"], "services": {
                    item["Config"]["Labels"][SERVICE_LABEL]: {
                        "status": item["State"]["Status"],
                        "health": item["State"].get("Health", {}).get("Status"),
                    } for item in containers
                }}

    def close(self) -> None:
        self.state = self._load_state()
        if self.state["closed"]:
            return
        self._prepare_mutation()
        if self.state["owned"]:
            # No global prune, rm, image deletion, or --remove-orphans operation.
            self._mutate(["down", "--volumes", "--timeout", "10"])
            if any(self._resources().values()):
                raise LabError("Some owned lab resources remain; broader cleanup is forbidden")
        self.state.update({"closed": True, "owned": False, "fault": None})
        self._save_state()


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated Docker diagnostic fault fixture")
    parser.add_argument("action", choices=("create", "up", "fault", "restore", "status", "down"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--fault", choices=FAULTS)
    args = parser.parse_args()
    try:
        if args.action == "create":
            config = create_lab()
            print(Path(config["secret_dir"]).parent / "config.json")
            return 0
        if args.config is None:
            parser.error("--config is required for an existing lab")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        lab = ComposeLab(config)
        if args.config.resolve() != lab.config_path.resolve():
            raise LabError("The config path must be this project's generated config.json")
        if args.action == "up":
            lab.up()
        elif args.action == "fault":
            if not args.fault:
                parser.error("--fault is required for fault injection")
            lab.fault(args.fault)
        elif args.action == "restore":
            lab.restore()
        elif args.action == "down":
            lab.close()
        print(json.dumps(lab.status(), ensure_ascii=False, indent=2))
        return 0
    except (LabError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Lab operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
