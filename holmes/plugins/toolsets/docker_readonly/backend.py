"""Bounded read-only Docker metadata, logs and fixed database/cache probes.

No Docker lifecycle, arbitrary command, SQL or destination API is exposed.
The sole exec operation is a fixed read-only helper in the generated probe container.
Targets are rediscovered and checked against generated ownership labels per call.
"""

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from holmes.plugins.toolsets.docker_readonly.security import (
    LAB_LABEL,
    PROJECT_LABEL,
    SERVICE_LABEL,
    BackendError,
    LabConfig,
    ScopeError,
    Service,
    redact,
)

DOCKER_TIMEOUT = 8.0
OUTPUT_LIMIT = 65536
CONFIG_KEYS = ("MYSQL_HOST", "MYSQL_PORT", "REDIS_HOST", "REDIS_PORT", "APP_MODE")
SERVICE_PORTS = {"api": "8000/tcp", "mysql": "3306/tcp", "redis": "6379/tcp"}
LAB_SERVICES = {"api", "mysql", "redis", "probe"}
HEX_ID = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class CommandOutput:
    stdout: str
    stderr: str = ""
    truncated: bool = False


class BoundedDockerRunner:
    """Use argv with shell=False; cap combined pipe bytes while they are read."""

    def run(self, args: list[str], *, allow_truncation: bool = False) -> CommandOutput:
        try:
            process = subprocess.Popen(
                ["docker", *args],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            raise BackendError("Local Docker CLI is unavailable.") from None
        buffers = [bytearray(), bytearray()]
        lock = threading.Lock()
        limit_hit = threading.Event()

        def read_pipe(pipe, index: int) -> None:
            try:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    with lock:
                        remaining = OUTPUT_LIMIT - sum(len(buffer) for buffer in buffers)
                        buffers[index].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            limit_hit.set()
                            process.kill()
                            break
            finally:
                pipe.close()

        readers = [
            threading.Thread(target=read_pipe, args=(process.stdout, 0), daemon=True),
            threading.Thread(target=read_pipe, args=(process.stderr, 1), daemon=True),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=DOCKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        if timed_out:
            raise BackendError("Local Docker read exceeded its 8 second timeout.")
        if any(reader.is_alive() for reader in readers):
            raise BackendError("Local Docker output pipes did not close within the read deadline.")
        if limit_hit.is_set() and not allow_truncation:
            raise BackendError("Local Docker metadata exceeded the 65536 byte output limit.")
        if process.returncode != 0 and not limit_hit.is_set():
            raise BackendError(f"Local Docker read failed (exit_code={process.returncode}).")
        return CommandOutput(
            stdout=buffers[0].decode("utf-8", errors="replace"),
            stderr=buffers[1].decode("utf-8", errors="replace"),
            truncated=limit_hit.is_set(),
        )


class DockerLabBackend:
    def __init__(self, config: LabConfig, runner: BoundedDockerRunner | None = None):
        self.lab = config
        self.runner = runner or BoundedDockerRunner()

    def _context(self) -> str:
        # DOCKER_HOST overrides context routing. Reject it rather than interpreting
        # precedence differently from Docker or allowing a local forwarding proxy.
        if os.environ.get("DOCKER_HOST"):
            raise ScopeError("DOCKER_HOST overrides are forbidden for the isolated lab.")
        result = self.runner.run(["context", "inspect"])
        contexts = self._json(result.stdout)
        if not isinstance(contexts, list) or len(contexts) != 1:
            raise ScopeError("Expected exactly one local Docker context.")
        context = contexts[0]
        endpoint = context.get("Endpoints", {}).get("docker", {}).get("Host", "")
        local = endpoint.startswith("unix:///") or bool(
            re.fullmatch(r"npipe:/{4}\.\/pipe/[A-Za-z0-9_.-]+", endpoint)
        )
        name = context.get("Name", "")
        if not local or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", name):
            raise ScopeError("Only a local unix socket or Windows named pipe Docker context is allowed.")
        return name

    @staticmethod
    def _json(text: str) -> Any:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            raise BackendError("Local Docker returned invalid bounded JSON metadata.") from None

    def _read(self, context: str, args: list[str], *, logs: bool = False) -> CommandOutput:
        if os.environ.get("DOCKER_HOST"):
            raise ScopeError("DOCKER_HOST overrides are forbidden for the isolated lab.")
        return self.runner.run(["--context", context, *args], allow_truncation=logs)

    def _check_labels(self, container: dict[str, Any], service: Service | None = None) -> None:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        if (
            labels.get(PROJECT_LABEL) != self.lab.project
            or labels.get(LAB_LABEL) != self.lab.lab_id
            or labels.get(SERVICE_LABEL) not in LAB_SERVICES
            or (service is not None and labels.get(SERVICE_LABEL) != service)
        ):
            raise ScopeError("Container ownership labels do not match the generated lab.")

    def _target(self, service: Service) -> tuple[str, dict[str, Any]]:
        if service not in LAB_SERVICES:
            raise ScopeError("Service is outside the fixed api/mysql/redis allowlist.")
        context = self._context()
        selection = self._read(
            context,
            [
                "ps", "--all", "--quiet", "--no-trunc",
                "--filter", f"label={PROJECT_LABEL}={self.lab.project}",
                "--filter", f"label={LAB_LABEL}={self.lab.lab_id}",
                "--filter", f"label={SERVICE_LABEL}={service}",
            ],
        )
        ids = selection.stdout.split()
        if len(ids) != 1 or not HEX_ID.fullmatch(ids[0]):
            raise ScopeError("Expected exactly one container with the generated lab service labels.")
        inspected = self._json(self._read(context, ["inspect", "--type", "container", ids[0]]).stdout)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ScopeError("Docker returned an ambiguous container inspection.")
        container = inspected[0]
        if container.get("Id") != ids[0]:
            raise ScopeError("Inspected container does not match the selected lab container.")
        self._check_labels(container, service)
        networks = container.get("NetworkSettings", {}).get("Networks", {})
        mode = container.get("HostConfig", {}).get("NetworkMode", "")
        if len(networks) != 1 or mode in ("host", "bridge", "none", "default") or mode.startswith("container:"):
            raise ScopeError("Lab container must use exactly one isolated project network.")
        network_id = next(iter(networks.values())).get("NetworkID", "")
        if not HEX_ID.fullmatch(network_id):
            raise ScopeError("Lab network identifier is invalid.")
        inspected_network = self._json(self._read(context, ["network", "inspect", network_id]).stdout)
        if not isinstance(inspected_network, list) or len(inspected_network) != 1:
            raise ScopeError("Docker returned an ambiguous project network inspection.")
        network = inspected_network[0]
        labels = network.get("Labels", {}) or {}
        if (
            network.get("Id") != network_id
            or network.get("Internal") is not True
            or labels.get(PROJECT_LABEL) != self.lab.project
            or labels.get(LAB_LABEL) != self.lab.lab_id
        ):
            raise ScopeError("Project network must be internal and carry the generated ownership labels.")
        members = list((network.get("Containers", {}) or {}).keys())
        if len(members) > 4 or any(not HEX_ID.fullmatch(member) for member in members):
            raise ScopeError("Project network contains unexpected members.")
        if members:
            peers = self._json(self._read(context, ["inspect", "--type", "container", *members]).stdout)
            if not isinstance(peers, list) or len(peers) != len(members):
                raise ScopeError("Project network members could not be verified.")
            if {peer.get("Id") for peer in peers} != set(members):
                raise ScopeError("Project network inspection returned unexpected members.")
            for peer in peers:
                self._check_labels(peer)
                peer_networks = peer.get("NetworkSettings", {}).get("Networks", {})
                peer_mode = peer.get("HostConfig", {}).get("NetworkMode", "")
                if (
                    len(peer_networks) != 1
                    or next(iter(peer_networks.values())).get("NetworkID") != network_id
                    or peer_mode in ("host", "bridge", "none", "default")
                    or peer_mode.startswith("container:")
                ):
                    raise ScopeError("Lab network contains a peer connected outside the isolated project network.")
        for port_map in (
            container.get("NetworkSettings", {}).get("Ports", {}) or {},
            container.get("HostConfig", {}).get("PortBindings", {}) or {},
        ):
            for bindings in port_map.values():
                for binding in bindings or []:
                    if binding.get("HostIp") != "127.0.0.1":
                        raise ScopeError("Lab published ports must bind exclusively to 127.0.0.1.")
        return context, container

    def _sanitize(self, data: Any) -> Any:
        return redact(data, self.lab.secrets())

    def health(self, service: Service) -> dict[str, Any]:
        _, container = self._target(service)
        state = container.get("State", {})
        return self._sanitize({
            "service": service,
            "state": {
                "status": state.get("Status", "unknown"),
                "running": state.get("Running") is True,
                "health": state.get("Health", {}).get("Status", "not_configured"),
                "exit_code": state.get("ExitCode"),
                "oom_killed": state.get("OOMKilled") is True,
            },
        })

    def logs(self, service: Service, tail: int, since_seconds: int, contains: str | None = None) -> dict[str, Any]:
        context, container = self._target(service)
        output = self._read(
            context,
            ["logs", "--timestamps", "--tail", str(tail), "--since", f"{since_seconds}s", container["Id"]],
            logs=True,
        )
        lines = (output.stdout + output.stderr).splitlines()
        # Literal filtering occurs locally. No filter ever becomes a shell expression.
        if contains is not None:
            lines = [line for line in lines if contains in line]
        truncated = output.truncated or len(lines) > tail
        return self._sanitize({
            "service": service, "lines": lines[-tail:], "truncated": truncated,
            "tail": tail, "since_seconds": since_seconds,
            "filter_applied": contains is not None,
        })

    def config(self) -> dict[str, Any]:
        _, container = self._target("api")
        values = {}
        for entry in container.get("Config", {}).get("Env", []) or []:
            name, separator, value = entry.partition("=")
            if separator and name in CONFIG_KEYS:
                values[name] = value[:200]
        return self._sanitize({"service": "api", "config": values})

    def _validate_probe(self, container: dict[str, Any]) -> None:
        config = container.get("Config", {})
        host = container.get("HostConfig", {})
        if config.get("Image") != f"{self.lab.project}-api:lab":
            raise ScopeError("Probe image must be the generated project API image.")
        if host.get("ReadonlyRootfs") is not True or host.get("Privileged") is True:
            raise ScopeError("Probe must use an unprivileged read-only root filesystem.")
        mounts = container.get("Mounts", [])
        expected = {"mysql_probe_password", "redis_probe_password"}
        actual = set()
        if len(mounts) != 2:
            raise ScopeError("Probe may mount only its two generated read-only credential files.")
        for mount in mounts:
            destination = mount.get("Destination", "")
            filename = destination.removeprefix("/run/secrets/")
            source = str(mount.get("Source", "")).replace("\\", "/").casefold()
            # Docker Desktop rewrites Windows source prefixes inside its VM.
            # The unique generated project suffix must still match exactly.
            suffix = f"/.runtime/{self.lab.project}/secrets/{filename}".casefold()
            if (
                destination != f"/run/secrets/{filename}"
                or filename not in expected or mount.get("Type") != "bind"
                or mount.get("RW") is not False or not source.endswith(suffix)
            ):
                raise ScopeError("Probe credential mount is outside the generated read-only secret allowlist.")
            actual.add(filename)
        if actual != expected:
            raise ScopeError("Probe credential mounts must contain each generated credential exactly once.")
        if container.get("State", {}).get("Running") is not True:
            raise BackendError("Generated probe helper is not running; connectivity evidence is unavailable.")

    def _probe(self, service: str) -> dict[str, Any]:
        if service not in ("mysql", "redis"):
            raise ScopeError("Only fixed MySQL and Redis probes are allowed.")
        _, target = self._target(service)
        check = "SELECT 1" if service == "mysql" else "PING"
        if target.get("State", {}).get("Running") is not True:
            return {"service": service, "reachable": False, "check": check, "error": "container_not_running"}
        context, probe = self._target("probe")
        self._validate_probe(probe)
        self.lab.secrets()
        output = self._read(
            context,
            ["exec", "--user", "10001:10001", probe["Id"], "python", "/app/probe.py", service],
        )
        data = self._json(output.stdout)
        allowed = {"service", "reachable", "check", "error", "error_code", "elapsed_ms"}
        if (
            not isinstance(data, dict) or set(data) - allowed
            or data.get("service") != service or data.get("check") != check
            or type(data.get("reachable")) is not bool
            or ("error" in data and (not isinstance(data["error"], str) or len(data["error"]) > 128))
            or ("error_code" in data and data["error_code"] is not None and type(data["error_code"]) is not int)
            or ("elapsed_ms" in data and (type(data["elapsed_ms"]) not in (int, float) or not 0 <= data["elapsed_ms"] <= 8000))
        ):
            raise BackendError("Generated probe helper returned an invalid bounded result schema.")
        return self._sanitize(data)

    def mysql(self) -> dict[str, Any]:
        return self._probe("mysql")

    def redis(self) -> dict[str, Any]:
        return self._probe("redis")
