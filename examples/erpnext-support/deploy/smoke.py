"""Own one fresh 8766 smoke container; verify no ERP calls and never expose passwords."""

import argparse
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from prepare import ROOT, reject_links


class NotReady(RuntimeError):
    pass


def smoke(image, runtime, keep=False):
    runtime = Path(runtime).absolute()
    reject_links(runtime)
    if not re.fullmatch(r"instance-[a-f0-9]{12}", runtime.name):
        raise ValueError("Smoke runtime must be freshly prepared")
    values = json.loads((runtime / "settings.json").read_text())
    if values["allowed_erp_origins"]:
        raise ValueError("Container smoke uses an empty ERP allowlist")
    if not re.fullmatch(r"holmes-erp-workbench:[A-Za-z0-9_.-]+", image):
        raise ValueError("Smoke requires the locally built scoped image")
    if os.environ.get("DOCKER_HOST"):
        raise ValueError("Remote Docker overrides are forbidden")
    initial = json.loads((runtime / "initial-login.json").read_text())
    context = "desktop-linux"
    def docker(args):
        result = subprocess.run(["docker", "--context", context, *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=120, shell=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).replace(initial["password"], "[REDACTED]")[-4000:])
        return result.stdout
    endpoint = json.loads(docker(["context", "inspect", context]))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("npipe:////./pipe/", "unix:///")):
        raise ValueError("Only the inspected local Docker context is allowed")
    protected_names = ["docker-learning-web-1", "my-site", "my-first-web",
                       *[f"holmes-lab-2c78e1e734d6-{service}-1" for service in ("api", "mysql", "redis", "probe")]]
    protected = {entry["Name"]: {"id": entry["Id"], "state": entry["State"]["Status"]}
                 for entry in json.loads(docker(["inspect", *protected_names]))}
    identity = secrets.token_hex(16)
    project = "holmes-workbench-" + identity[:12]
    network, name = project + "_outbound", project + "-workbench-1"
    label = "io.holmes.workbench=" + identity
    if docker(["ps", "-aq", "--filter", f"name=^/{name}$"]).strip():
        raise ValueError("Existing containers cannot be adopted")
    if docker(["network", "ls", "-q", "--filter", f"name=^{network}$"]).strip():
        raise ValueError("Existing networks cannot be adopted")
    network_id = docker(["network", "create", "--label", label, network]).strip()
    container_id = None
    records = []
    success = False
    started = time.perf_counter()
    try:
        container_id = docker(["run", "--detach", "--name", name, "--label", label,
                               "--network", network, "--user", "10001:10001", "--read-only",
                               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                               "--tmpfs", "/tmp:size=64m,mode=1777", "--memory", "1536m",
                               "--publish", "127.0.0.1:8766:8765",
                               "--mount", f"type=bind,source={runtime},target=/state", image]).strip()
        for attempt in Retrying(stop=stop_after_delay(180), wait=wait_fixed(3),
                                retry=retry_if_exception_type((requests.RequestException, NotReady)), reraise=True):
            with attempt:
                with requests.Session() as session:
                    session.trust_env = False
                    result = session.get("http://127.0.0.1:8766/health", timeout=3, allow_redirects=False)
                if result.status_code != 200 or result.json().get("status") != "ok":
                    raise NotReady("Container is not healthy yet")
        with requests.Session() as session:
            session.trust_env = False
            login = session.post("http://127.0.0.1:8766/api/session", json=initial,
                                 headers={"Origin": "http://127.0.0.1:8766"}, timeout=10, allow_redirects=False)
            assert login.status_code == 200 and login.json()["user"]["role"] == "admin"
            records.append({"case": "random_login", "status": login.status_code, "passed": True})
            result = session.get("http://127.0.0.1:8766/api/session", timeout=10, allow_redirects=False)
            assert result.status_code == 200
            records.append({"case": "session_cookie", "status": result.status_code, "passed": True})
            result = session.get("http://127.0.0.1:8766/", timeout=10, allow_redirects=False)
            assert result.status_code == 200 and "<html" in result.text and "/assets/" in result.text
            records.append({"case": "built_frontend", "status": result.status_code, "passed": True})
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', result.text)
            assert assets and any(path.endswith(".js") for path in assets)
            for asset in assets:
                assert re.fullmatch(r"/assets/[A-Za-z0-9_./-]+\.(?:js|css)", asset) and ".." not in asset
                response = session.get("http://127.0.0.1:8766" + asset, timeout=10, allow_redirects=False)
                assert response.status_code == 200 and response.content
            records.append({"case": "compiled_frontend_assets", "count": len(assets), "passed": True})
            result = session.get("http://127.0.0.1:8766/api/environments", timeout=10, allow_redirects=False)
            assert result.status_code == 200 and result.json() == {"items": []}
            records.append({"case": "empty_customer_environment", "status": result.status_code, "passed": True})
        container = json.loads(docker(["inspect", container_id]))[0]
        for attempt in Retrying(stop=stop_after_delay(30), wait=wait_fixed(2),
                                retry=retry_if_exception_type(NotReady), reraise=True):
            with attempt:
                container = json.loads(docker(["inspect", container_id]))[0]
                if container["State"]["Health"]["Status"] != "healthy":
                    raise NotReady("Docker healthcheck has not succeeded yet")
        assert container["Config"]["User"] == "10001:10001"
        assert container["HostConfig"]["ReadonlyRootfs"] is True
        assert container["HostConfig"]["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in container["HostConfig"]["SecurityOpt"]
        assert len(container["Mounts"]) == 1 and container["Mounts"][0]["Destination"] == "/state"
        assert container["Mounts"][0]["Type"] == "bind"
        assert set(container["NetworkSettings"]["Networks"]) == {network}
        network_info = json.loads(docker(["network", "inspect", network_id]))[0]
        assert network_info["Labels"].get("io.holmes.workbench") == identity
        assert set(network_info["Containers"]) == {container_id}
        assert container["NetworkSettings"]["Ports"]["8765/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": "8766"}]
        versions = json.loads(docker(["exec", "--user", "10001:10001", container_id,
                                      "python", "-c", "import sys,importlib.metadata,json; print(json.dumps({'python':sys.version.split()[0],'litellm':importlib.metadata.version('litellm'),'fastapi':importlib.metadata.version('fastapi')}))"]))
        assert versions["python"] == "3.12.10"
        after = {entry["Name"]: {"id": entry["Id"], "state": entry["State"]["Status"]}
                 for entry in json.loads(docker(["inspect", *protected_names]))}
        assert after == protected
        report = {"kind": "real_workbench_container_smoke", "utc": datetime.now(timezone.utc).isoformat(),
                  "image": image, "image_id": container["Image"], "runtime": runtime.name,
                  "erp_calls": 0, "model_invoked": False, "elapsed_seconds": round(time.perf_counter() - started, 3),
                  "health_http_status": 200, "checks": records, "versions": versions,
                  "docker_health_status": container["State"]["Health"]["Status"],
                  "protected_existing_containers_unchanged": True,
                  "readonly_root": True, "nonroot_user": "10001:10001", "docker_socket_mounted": False,
                  "capabilities_dropped": ["ALL"], "no_new_privileges": True,
                  "only_state_bind_mount": True, "only_owned_network": True}
        path = ROOT / ("container-validation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        with path.open("x") as stream:
            json.dump(report, stream, indent=2)
        if keep:
            with (runtime / "owned-smoke.json").open("x") as stream:
                json.dump({"container_id": container_id, "network_id": network_id,
                           "identity": identity, "name": name, "network": network}, stream)
        success = True
        print(json.dumps({"report": str(path), "passed": True, "erp_calls": 0, "image": image}))
        return path
    finally:
        # Only IDs returned by this run and whose label still matches can go.
        if container_id and not (keep and success):
            item = json.loads(docker(["inspect", container_id]))[0]
            if item["Config"]["Labels"].get("io.holmes.workbench") != identity or item["Name"].lstrip("/") != name:
                raise ValueError("Refusing to remove an unowned smoke container")
            docker(["stop", "--time", "10", container_id])
            docker(["rm", container_id])
        if not (keep and success):
            item = json.loads(docker(["network", "inspect", network_id]))[0]
            if item["Labels"].get("io.holmes.workbench") != identity or item.get("Containers"):
                raise ValueError("Refusing to remove a foreign or nonempty smoke network")
            docker(["network", "rm", network_id])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="Keep a successful owned instance for browser verification")
    args = parser.parse_args()
    smoke(args.image, args.runtime, args.keep)
