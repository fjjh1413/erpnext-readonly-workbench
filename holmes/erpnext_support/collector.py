"""Optional local ERP lab collector. Its HTTP API has no Docker command input.

Only this service accesses the local daemon. Workbench and Agent receive a
small GET snapshot, never a socket, raw inspect output or host command tool.
Customer collectors require a separately audited integration.
"""

import argparse
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from holmes.erpnext_support.security import safe_path, sanitize
from holmes.erpnext_support.settings import EXAMPLE, restrict_runtime
from holmes.plugins.toolsets.docker_readonly.backend import BoundedDockerRunner


HEX = re.compile(r"^[a-f0-9]{64}$")
SERVICES = {"backend": "application", "frontend": "application", "db": "database",
            "redis-cache": "redis", "redis-queue": "redis"}


class LabCollector:
    def __init__(self, config_path: Path, runner=None):
        self.path = safe_path(EXAMPLE / "lab/.runtime", config_path)
        self.config = json.loads(self.path.read_text(encoding="utf-8"))
        self.project = self.config["project"]
        if not re.fullmatch(r"holmes-erp-[a-f0-9]{12}", self.project):
            raise ValueError("Unsupported collector namespace")
        if not re.fullmatch(r"[a-f0-9]{32}", self.config["lab_id"]):
            raise ValueError("Invalid lab identity")
        if self.path != EXAMPLE / "lab/.runtime" / self.project / "config.json":
            raise ValueError("Lab configuration path does not match project")
        self.runner = runner or BoundedDockerRunner()
        self.secrets = []
        for name in ("db_root_password", "admin_password"):
            self.secrets.append(safe_path(self.path.parent, self.path.parent / "secrets" / name).read_text().strip())
        credential_file = self.path.parent / "secret.json"
        if credential_file.exists():
            self.secrets.extend(json.loads(safe_path(self.path.parent, credential_file).read_text()).values())

    def read(self, args: list[str], logs=False):
        if os.environ.get("DOCKER_HOST"):
            raise ValueError("Remote Docker overrides are forbidden")
        return self.runner.run(["--context", self.config["context"], *args], allow_truncation=logs)

    def snapshot(self) -> dict:
        context = self.config["context"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", context):
            raise ValueError("Invalid Docker context")
        entries = json.loads(self.read(["context", "inspect", context]).stdout)
        endpoint = entries[0]["Endpoints"]["docker"]["Host"] if len(entries) == 1 else ""
        if not (endpoint.startswith("unix:///") or re.fullmatch(r"npipe:/{4}\.\/pipe/[A-Za-z0-9_.-]+", endpoint)):
            raise ValueError("Only local Docker transport is permitted")
        ownership = json.loads(safe_path(self.path.parent, self.path.parent / "ownership.json").read_text())
        if not ownership.get("owned") or ownership.get("closed"):
            raise ValueError("Lab ownership is inactive")
        ids = self.read(["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project]).stdout.split()
        if not ids or len(ids) > 10 or any(not HEX.fullmatch(i) for i in ids) or set(ids) != set(ownership["containers"]):
            raise ValueError("Container ownership changed; collector denied")
        # Project fixed metadata inside Docker's formatter: environment,
        # healthcheck output and command lines never leave the daemon.
        projection = '{"id":{{json .Id}},"labels":{{json .Config.Labels}},"networks":{{json .NetworkSettings.Networks}},"state":{{json .State.Status}},"running":{{json .State.Running}},"health":{{with index .State "Health"}}{{json .Status}}{{else}}"not_configured"{{end}},"restart_count":{{json .RestartCount}}}'
        containers = [json.loads(line) for line in self.read(["inspect", "--type", "container", "--format", projection, *ids]).stdout.splitlines()]
        if {c["id"] for c in containers} != set(ids):
            raise ValueError("Inspected identities differ from owned containers")
        network_projection = '{"id":{{json .Id}},"labels":{{json .Labels}},"containers":{{json .Containers}}}'
        networks = [json.loads(line) for line in self.read(["network", "inspect", "--format", network_projection, *ownership["networks"]]).stdout.splitlines()]
        if {n["id"] for n in networks} != set(ownership["networks"]):
            raise ValueError("Network ownership changed")
        for network in networks:
            if (network["labels"].get("io.holmes.erp-lab") != self.config["lab_id"] or
                    network["labels"].get("com.docker.compose.project") != self.project or
                    not set(network["containers"] or {}).issubset(set(ids))):
                raise ValueError("An owned network contains an unauthorized peer")
        services = {}
        for container in containers:
            labels = container["labels"] or {}
            if (labels.get("com.docker.compose.project") != self.project or
                    labels.get("io.holmes.erp-lab") != self.config["lab_id"]):
                raise ValueError("Container labels do not match authorization")
            networks = container["networks"]
            if not networks or any(n.get("NetworkID") not in ownership["networks"] for n in networks.values()):
                raise ValueError("Container is outside its owned networks")
            service = labels.get("com.docker.compose.service")
            if service not in SERVICES:
                continue
            if service in services:
                raise ValueError("Ambiguous service identity")
            value = {"category": SERVICES[service], "state": container["state"],
                     "health": container["health"],
                     "restart_count": container["restart_count"], "ping": "not_checked"}
            if service.startswith("redis-") and container["running"]:
                # The only exec is a literal read-only Redis PING; no arguments
                # or destination are accepted from the Agent or HTTP client.
                value["ping"] = "PONG" if self.read(["exec", container["id"], "redis-cli", "PING"]).stdout.strip() == "PONG" else "failed"
            services[service] = value
        if set(services) != set(SERVICES):
            raise ValueError("Required service metadata is missing")
        backend = next(c for c in containers if c["labels"].get("com.docker.compose.service") == "backend")
        logs = self.read(["logs", "--timestamps", "--tail", "50", "--since", "900s", backend["id"]], logs=True)
        return sanitize({"source": "local-restricted-erp-collector", "query": {"project": self.project,
                         "services": list(SERVICES), "log_tail": 50, "log_window_seconds": 900},
                         "collected_at": datetime.now(timezone.utc).isoformat(), "business_at": None,
                         "payload": {"services": services, "application_logs": (logs.stdout + logs.stderr)[-12000:]},
                         "limitations": ["运行状态和固定PING不能证明ERP应用连接路径或发货业务完成。",
                                         "运行日志按时间窗口采集，尚未证明与指定发货单相关。"] +
                                        (["运行日志超过限制，已截断。"] if logs.truncated else [])}, self.secrets)


def create_collector(collector: LabCollector, token: str) -> FastAPI:
    if not isinstance(token, str) or not token.isascii() or not 32 <= len(token) <= 256:
        raise ValueError("A nonempty controlled collector token is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/snapshot")
    def snapshot(request: Request):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not supplied.isascii() or not hmac.compare_digest(supplied, token) or request.query_params:
            raise HTTPException(403, "Collector access denied")
        try:
            return collector.snapshot()
        except Exception as error:
            raise HTTPException(503, "Scoped snapshot unavailable (" + type(error).__name__ + ")") from None

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated ERP read-only collector")
    parser.add_argument("--lab-config", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    directory = args.runtime.absolute()
    safe_path(EXAMPLE / ".runtime", directory)
    directory.mkdir(exist_ok=True)
    restrict_runtime(directory)
    path = safe_path(directory, directory / "collector.json")
    if not path.exists():
        with path.open("x", encoding="utf-8") as output:
            json.dump({"base_url": "http://127.0.0.1:8767", "token": secrets.token_urlsafe(32)}, output)
    credentials = json.loads(path.read_text())
    if (path.stat().st_size > 4096 or set(credentials) != {"base_url", "token"} or
            credentials["base_url"] != "http://127.0.0.1:8767"):
        raise ValueError("Controlled collector configuration is invalid")
    uvicorn.run(create_collector(LabCollector(args.lab_config), credentials["token"]),
                host="127.0.0.1", port=8767, access_log=False)


if __name__ == "__main__":
    main()
