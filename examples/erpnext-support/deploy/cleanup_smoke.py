"""Remove only the successful smoke resources recorded by this module."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from prepare import reject_links


def cleanup(runtime):
    runtime = Path(runtime).absolute()
    reject_links(runtime)
    path = runtime / "owned-smoke.json"
    reject_links(path)
    data = json.loads(path.read_text())
    if os.environ.get("DOCKER_HOST"):
        raise ValueError("Remote Docker overrides are forbidden")
    if not re.fullmatch(r"[a-f0-9]{32}", data["identity"]):
        raise ValueError("Invalid smoke identity")
    project = "holmes-workbench-" + data["identity"][:12]
    if data["name"] != project + "-workbench-1" or data["network"] != project + "_outbound":
        raise ValueError("Invalid smoke namespace")
    for name in ("container_id", "network_id"):
        if not re.fullmatch(r"[a-f0-9]{64}", data[name]):
            raise ValueError("Invalid recorded resource identifier")
    def docker(args):
        result = subprocess.run(["docker", "--context", "desktop-linux", *args], capture_output=True,
                                text=True, timeout=60, shell=False, check=True)
        return result.stdout
    endpoint = json.loads(docker(["context", "inspect", "desktop-linux"]))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("npipe:////./pipe/", "unix:///")):
        raise ValueError("Only the verified local daemon is allowed")
    container = json.loads(docker(["inspect", data["container_id"]]))[0]
    network = json.loads(docker(["network", "inspect", data["network_id"]]))[0]
    if (container["Name"].lstrip("/") != data["name"]
            or container["Config"]["Labels"].get("io.holmes.workbench") != data["identity"]
            or set(container["NetworkSettings"]["Networks"]) != {data["network"]}
            or network["Name"] != data["network"]
            or network["Labels"].get("io.holmes.workbench") != data["identity"]
            or set(network.get("Containers", {})) - {data["container_id"]}):
        raise ValueError("Foreign resource: no cleanup performed")
    docker(["stop", "--time", "10", data["container_id"]])
    docker(["rm", data["container_id"]])
    docker(["network", "rm", data["network_id"]])
    print(json.dumps({"removed_owned_smoke_only": True, "runtime_preserved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    cleanup(parser.parse_args().runtime)
