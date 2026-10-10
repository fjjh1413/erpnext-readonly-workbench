"""Fixed local service checks; no user-controlled endpoint or credentials output."""

import argparse
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_ready(service: str) -> bool:
    if service not in {"api", "worker"}:
        raise ValueError("Unknown fixed service")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    endpoint = "http://127.0.0.1:8765/ready" if service == "api" else "http://api:8765/ready"
    request = urllib.request.Request(endpoint, headers={"Host": "localhost"})
    with opener.open(request, timeout=3) as response:
        body = response.read(4097)
        if len(body) > 4096 or response.status != 200:
            return False
        value = json.loads(body)
        return isinstance(value, dict) and all(value.get(name) is True for name in (
            "ready", "database_ready", "broker_ready", "worker_ready"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=("api", "worker"))
    args = parser.parse_args()
    try:
        passed = check_ready(args.service)
    except Exception:
        passed = False
    raise SystemExit(0 if passed else 1)
