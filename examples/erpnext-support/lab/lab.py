"""Isolated ERP fixture lifecycle. Never registered as an Agent tool."""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

ROOT = Path(__file__).resolve().parent
LABEL = "io.holmes.erp-lab"
PROJECT_LABEL = "com.docker.compose.project"
SERVICES = {"db", "redis-cache", "redis-queue", "configurator", "create-site", "backend",
            "frontend", "websocket", "worker", "scheduler"}


class ScopeError(RuntimeError):
    pass


class NotReady(RuntimeError):
    pass


def safe_path(path):
    candidate = Path(path).absolute()
    if not candidate.resolve().is_relative_to(ROOT.resolve()):
        raise ScopeError("Runtime path outside this lab")
    for entry in (candidate, *candidate.parents):
        if entry.is_symlink() or entry.is_junction():
            raise ScopeError("Runtime links and junctions are forbidden")
        if entry == ROOT:
            break


def write_new(path, value):
    safe_path(path)
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)


def create():
    safe_path(ROOT / ".runtime")
    (ROOT / ".runtime").mkdir(exist_ok=True)
    identity = secrets.token_hex(16)
    project = "holmes-erp-" + identity[:12]
    directory = ROOT / ".runtime" / project
    directory.mkdir(mode=0o700)
    if os.name == "nt":
        who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        sid = re.search(r"S-1-5-[0-9-]+", who.stdout).group()
        subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r",
                        f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True, check=True)
    secret_dir = directory / "secrets"
    secret_dir.mkdir(mode=0o700)
    for name in ("db_root_password", "admin_password"):
        with (secret_dir / name).open("x") as stream:
            stream.write(secrets.token_hex(32))
        (secret_dir / name).chmod(0o444)
    config = {"project": project, "lab_id": identity, "secret_dir": str(secret_dir), "context": "desktop-linux"}
    write_new(directory / "config.json", config)
    write_new(directory / "ownership.json", {"owned": False, "closed": False,
               "containers": [], "networks": [], "volumes": []})
    (directory / "compose.env").touch(exist_ok=False)
    return directory / "config.json"


class ERPLab:
    def __init__(self, config_path):
        self.path = Path(config_path)
        safe_path(self.path)
        self.config = json.loads(self.path.read_text())
        self.project = self.config["project"]
        self.lab_id = self.config["lab_id"]
        if not re.fullmatch(r"holmes-erp-[a-f0-9]{12}", self.project) or not re.fullmatch(r"[a-f0-9]{32}", self.lab_id):
            raise ScopeError("Invalid isolated ERP namespace")
        self.directory = ROOT / ".runtime" / self.project
        safe_path(self.directory)
        if self.path.resolve() != self.directory / "config.json":
            raise ScopeError("Configuration must belong to the fresh project runtime")
        self.secret_dir = self.directory / "secrets"
        safe_path(self.secret_dir)
        self.state_path = self.directory / "ownership.json"
        safe_path(self.state_path)
        self.state = json.loads(self.state_path.read_text())
        self.context = self.config["context"]
        self.passwords = []
        for name in ("db_root_password", "admin_password"):
            safe_path(self.secret_dir / name)
            self.passwords.append((self.secret_dir / name).read_text().strip())
        credential_path = self.directory / "secret.json"
        safe_path(credential_path)
        if credential_path.exists():
            self.passwords.extend(json.loads(credential_path.read_text()).values())
        if os.environ.get("DOCKER_HOST"):
            raise ScopeError("DOCKER_HOST overrides are forbidden")
        contexts = json.loads(self.run(["context", "inspect", self.context]))
        endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        if not (endpoint.startswith("unix:///") or re.fullmatch(r"npipe:/{4}\.\/pipe/[A-Za-z0-9_.-]+", endpoint)):
            raise ScopeError("Only the verified local Docker context is allowed")

    def save(self):
        safe_path(self.state_path)
        pending = self.state_path.with_suffix(".pending")
        write_new(pending, self.state)
        pending.replace(self.state_path)

    def run(self, arguments, timeout=60):
        if os.environ.get("DOCKER_HOST"):
            raise ScopeError("DOCKER_HOST overrides are forbidden")
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("COMPOSE_", "ERP_"))}
        environment.update({"ERP_LAB_ID": self.lab_id, "ERP_SECRET_DIR": self.secret_dir.as_posix()})
        result = subprocess.run(["docker", "--context", self.context, *arguments], cwd=ROOT,
                                env=environment, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, shell=False)
        text = result.stderr or result.stdout if result.returncode else result.stdout
        for password in self.passwords:
            text = text.replace(password, "[REDACTED]")
        if result.returncode:
            raise RuntimeError(text[-6000:])
        return text

    def compose(self, arguments, timeout=60):
        return self.run(["compose", "--project-name", self.project, "--project-directory", str(ROOT),
                         "--file", str(ROOT / "compose.yaml"), "--env-file", str(self.directory / "compose.env"),
                         *arguments], timeout)

    def resources(self):
        scope = f"label={PROJECT_LABEL}={self.project}"
        return {"containers": self.run(["ps", "-aq", "--no-trunc", "--filter", scope]).split(),
                "networks": self.run(["network", "ls", "-q", "--no-trunc", "--filter", scope]).split(),
                "volumes": self.run(["volume", "ls", "-q", "--filter", scope]).split()}

    def inspected(self, kind, identifiers):
        if not identifiers:
            return []
        prefix = [] if kind == "containers" else ["network" if kind == "networks" else "volume"]
        return json.loads(self.run([*prefix, "inspect", *identifiers]))

    def validate_resource(self, kind, item):
        labels = item["Config"].get("Labels", {}) if kind == "containers" else item.get("Labels", {})
        if labels.get(LABEL) != self.lab_id or labels.get(PROJECT_LABEL) != self.project:
            raise ScopeError("Resource ownership labels mismatch")
        if kind == "containers":
            service = labels.get("com.docker.compose.service")
            if service not in SERVICES or item["Name"].lstrip("/") != f"{self.project}-{service}-1":
                raise ScopeError("Unexpected container in this ERP project")
            expected = {f"{self.project}_core", f"{self.project}_entry"} if service == "frontend" else {f"{self.project}_core"}
            if set(item["NetworkSettings"]["Networks"]) != expected:
                raise ScopeError("ERP container joined a foreign network")
        elif kind == "networks":
            if item["Name"] not in {f"{self.project}_core", f"{self.project}_entry"}:
                raise ScopeError("Unexpected ERP network")
            if item["Name"].endswith("_core") and item["Internal"] is not True:
                raise ScopeError("ERP core network must remain internal")
        elif item["Name"] not in {f"{self.project}_{name}" for name in ("sites", "logs", "db-data", "redis-data")}:
            raise ScopeError("Unexpected ERP data volume")

    def validate_manifest(self):
        manifest = json.loads(self.compose(["config", "--format", "json"]))
        if set(manifest.get("services", {})) != SERVICES or set(manifest.get("networks", {})) != {"core", "entry"}:
            raise ScopeError("Unexpected ERP fixture services or networks")
        for name, network in manifest["networks"].items():
            if network.get("external") or network.get("driver_opts") or network.get("name") != f"{self.project}_{name}":
                raise ScopeError("Shared or overridden ERP networks are forbidden")
            if network.get("labels", {}).get(LABEL) != self.lab_id:
                raise ScopeError("ERP networks require ownership labels")
        if manifest["networks"]["core"].get("internal") is not True:
            raise ScopeError("ERP core network must remain internal")
        expected_volumes = {"sites", "logs", "db-data", "redis-data"}
        if set(manifest.get("volumes", {})) != expected_volumes:
            raise ScopeError("Unexpected ERP volumes")
        for name, volume in manifest["volumes"].items():
            if volume.get("external") or volume.get("driver_opts") or volume.get("driver") not in (None, "local"):
                raise ScopeError("External or remote ERP volumes are forbidden")
            if volume.get("name") != f"{self.project}_{name}" or volume.get("labels", {}).get(LABEL) != self.lab_id:
                raise ScopeError("ERP volumes must belong to this runtime")
        if set(manifest.get("secrets", {})) != {"db_root_password", "admin_password"}:
            raise ScopeError("Unexpected ERP secret inputs")
        for name, secret in manifest["secrets"].items():
            if Path(secret.get("file", "")).resolve() != self.secret_dir / name or secret.get("external"):
                raise ScopeError("ERP secrets must come from this private runtime")
        for name, service in manifest["services"].items():
            expected_image = "mariadb:10.6.22" if name == "db" else "redis:6.2.18-alpine" if name.startswith("redis-") else "frappe/erpnext:v15.85.1"
            if service.get("image") != expected_image or service.get("labels", {}).get(LABEL) != self.lab_id:
                raise ScopeError("Only the fixed official ERP images may be used")
            expected_networks = {"core", "entry"} if name == "frontend" else {"core"}
            if set(service.get("networks", {})) != expected_networks:
                raise ScopeError("Service attached outside its ERP networks")
            if any(service.get(key) for key in ("network_mode", "privileged", "pid", "ipc", "extra_hosts", "devices", "volumes_from", "cap_add")):
                raise ScopeError("Host privileges or namespace overrides are forbidden")
            expected_secrets = {"db_root_password"} if name == "db" else {"db_root_password", "admin_password"} if name == "create-site" else set()
            if {entry["source"] for entry in service.get("secrets", [])} != expected_secrets:
                raise ScopeError("Credentials may only reach the fixture initialization services")
            ports = service.get("ports", [])
            if name == "frontend":
                if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or ports[0].get("target") != 8080 or str(ports[0].get("published")) != "8090":
                    raise ScopeError("Only the fixed loopback frontend port 8090 is allowed")
            elif ports:
                raise ScopeError("ERP database, cache and backend ports must not be published")
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    if Path(mount["source"]).resolve() not in {ROOT / "scripts", ROOT / "frappe_app"} or mount.get("read_only") is not True:
                        raise ScopeError("Only read-only local fixture source mounts are allowed")
                elif mount["type"] == "volume":
                    if mount["source"] not in expected_volumes:
                        raise ScopeError("Service references an unexpected data volume")
                else:
                    raise ScopeError("Unexpected ERP service mount")

    def check(self):
        if self.state["closed"]:
            raise ScopeError("This lab is closed; create a fresh runtime")
        resources = self.resources()
        if not self.state["owned"] and any(resources.values()):
            raise ScopeError("Existing resources can never be adopted")
        for kind, identifiers in resources.items():
            if not set(identifiers).issubset(self.state[kind]):
                raise ScopeError("Unrecorded resource in this project")
            for item in self.inspected(kind, identifiers):
                self.validate_resource(kind, item)
                if kind == "networks" and not set(item.get("Containers", {})).issubset(self.state["containers"]):
                    raise ScopeError("Foreign container attached to the ERP network")

    def capture(self):
        resources = self.resources()
        for kind, identifiers in resources.items():
            for item in self.inspected(kind, identifiers):
                self.validate_resource(kind, item)
            self.state[kind] = identifiers
        self.save()

    def mutate(self, arguments, timeout=120):
        self.validate_manifest()
        self.check()
        try:
            self.compose(arguments, timeout)
        finally:
            self.capture()

    def container(self, service):
        self.check()
        matches = [item for item in self.inspected("containers", self.state["containers"])
                   if item["Config"]["Labels"]["com.docker.compose.service"] == service]
        if len(matches) != 1:
            raise ScopeError("Expected exactly one scoped ERP service")
        return matches[0]

    def url(self):
        ports = self.container("frontend")["NetworkSettings"]["Ports"]["8080/tcp"]
        if len(ports) != 1 or ports[0]["HostIp"] != "127.0.0.1":
            raise ScopeError("ERP frontend must be loopback-only")
        return "http://127.0.0.1:" + ports[0]["HostPort"]

    def up(self):
        self.check()
        self.state["owned"] = True
        self.save()
        self.mutate(["up", "--detach"], timeout=1500)
        for attempt in Retrying(stop=stop_after_delay(180), wait=wait_fixed(2),
                                retry=retry_if_exception_type((NotReady, requests.RequestException)), reraise=True):
            with attempt:
                with requests.Session() as session:
                    session.trust_env = False
                    result = session.get(self.url() + "/api/method/ping", timeout=5, allow_redirects=False)
                if result.status_code != 200 or result.json() != {"message": "pong"}:
                    raise NotReady("ERP HTTP endpoint is not ready")
        backend = self.container("backend")["Id"]
        for source, destination in (("holmes_seed_result.json", "seed-result.json"),
                                    ("holmes_diagnosis_secret.json", "secret.json")):
            target = self.directory / destination
            safe_path(target)
            if not target.exists():
                self.run(["cp", f"{backend}:/home/frappe/frappe-bench/sites/frontend/private/{source}", str(target)])
        connection = {"base_url": self.url(),
                  "credential_file": str(self.directory / "secret.json"), "site": "frontend",
                  "erpnext_version": "15.85.1", "frappe_version": "15.87.0", "project": self.project}
        connection_path = self.directory / "connection.json"
        safe_path(connection_path)
        if connection_path.exists():
            if json.loads(connection_path.read_text()) != connection:
                raise ScopeError("Existing connection evidence differs; replacement is forbidden")
        else:
            write_new(connection_path, connection)
        return {"base_url": self.url(), "connection_file": str(self.directory / "connection.json"),
                "seed_result": str(self.directory / "seed-result.json")}

    def status(self):
        self.check()
        return {"project": self.project, "base_url": self.url(), "services": {
            item["Config"]["Labels"]["com.docker.compose.service"]: item["State"]["Status"]
            for item in self.inspected("containers", self.state["containers"])}}

    def fault(self, name):
        service = {"database_unavailable": "db", "cache_unavailable": "redis-cache", "api_unavailable": "backend"}.get(name)
        if service is None:
            raise ScopeError("Unsupported isolated ERP fault")
        self.mutate(["stop", "--timeout", "10", service])

    def restore(self):
        self.mutate(["start", "db", "redis-cache", "backend"])

    def manual_recheck(self):
        for name in ("recheck-result.json", "limited-secret.json", "denied-secret.json"):
            safe_path(self.directory / name)
            if (self.directory / name).exists():
                raise ScopeError("The manual-action fixture has already been exported")
        self.mutate(["run", "--rm", "--no-deps", "--entrypoint",
                     "/home/frappe/frappe-bench/env/bin/python", "create-site", "/opt/lab/recheck.py"], timeout=900)
        return self.export_recheck()

    def export_recheck(self):
        backend = self.container("backend")["Id"]
        for source, name in (("holmes_recheck_result.json", "recheck-result.json"),
                             ("holmes_limited_secret.json", "limited-secret.json"),
                             ("holmes_denied_secret.json", "denied-secret.json")):
            target = self.directory / name
            safe_path(target)
            if target.exists():
                raise ScopeError("Refusing to replace manual-action evidence or credentials")
            self.run(["cp", f"{backend}:/home/frappe/frappe-bench/sites/frontend/private/{source}", str(target)])
        return {"report": str(self.directory / "recheck-result.json"), "agent_writes": False}

    def down(self):
        self.mutate(["down", "--volumes", "--timeout", "10"])
        if any(self.resources().values()):
            raise ScopeError("Owned ERP resources remain; wider cleanup is forbidden")
        self.state.update({"owned": False, "closed": True})
        self.save()


def main():
    parser = argparse.ArgumentParser(description="Fresh scoped ERPNext v15 fixture")
    parser.add_argument("action", choices=("create", "up", "status", "fault", "restore", "manual_recheck", "down"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--fault", choices=("database_unavailable", "cache_unavailable", "api_unavailable"))
    args = parser.parse_args()
    if args.action == "create":
        print(create())
        return
    if not args.config:
        parser.error("--config is required")
    lab = ERPLab(args.config)
    if args.action == "fault":
        if not args.fault:
            parser.error("--fault is required")
        lab.fault(args.fault)
    else:
        result = getattr(lab, args.action)()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
