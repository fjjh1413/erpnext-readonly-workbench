"""Fixed read-only internal probes. There is no endpoint, SQL or command input."""

import argparse
import json
import re
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pymysql
import redis

SECRET_DIR = Path("/run/secrets")
MAX_RESPONSE_BYTES = 4096


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def password(name: str) -> str:
    with (SECRET_DIR / name).open(encoding="utf-8") as stream:
        value = stream.read(65).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid runtime credential file")
    return value


def mysql_check() -> dict[str, Any]:
    result: dict[str, Any] = {"service": "mysql", "check": "SELECT 1"}
    start = time.perf_counter()
    try:
        with pymysql.connect(
            host="mysql", port=3306, user="probe", password=password("mysql_probe_password"),
            connect_timeout=2, read_timeout=2, write_timeout=2, autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise ValueError("Unexpected fixed query result")
        result.update({"reachable": True})
    except (OSError, ValueError, pymysql.MySQLError) as exc:
        result.update({"reachable": False, "error": "connection_failed"})
        if isinstance(exc, pymysql.MySQLError) and exc.args and isinstance(exc.args[0], int):
            result["error_code"] = exc.args[0]
            if exc.args[0] == 1045:
                result["error"] = "authentication_failed"
        elif isinstance(exc, TimeoutError):
            result["error"] = "timeout"
        elif isinstance(exc, ValueError):
            result["error"] = "unexpected_response"
    result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
    return result


def redis_check() -> dict[str, Any]:
    result: dict[str, Any] = {"service": "redis", "check": "PING"}
    start = time.perf_counter()
    try:
        with redis.Redis(
            host="redis", port=6379, username="probe", password=password("redis_probe_password"),
            socket_connect_timeout=2, socket_timeout=2, protocol=2,
        ) as connection:
            if connection.ping() is not True:
                raise ValueError("Unexpected fixed PING response")
        result.update({"reachable": True})
    except (OSError, ValueError, redis.RedisError) as exc:
        result.update({"reachable": False, "error": "connection_failed"})
        if isinstance(exc, redis.AuthenticationError):
            result["error"] = "authentication_failed"
        elif isinstance(exc, (TimeoutError, redis.TimeoutError)):
            result["error"] = "timeout"
        elif isinstance(exc, ValueError):
            result["error"] = "unexpected_response"
    result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
    return result


def ready_check() -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        try:
            response = opener.open("http://api:8000/ready", timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("Readiness response exceeds the fixed size limit")
            decoded = json.loads(payload)
            # Preserve only the known readiness schema; no arbitrary server
            # error text, response headers, URLs or credentials can leave here.
            if not isinstance(decoded, dict) or decoded.get("status") not in ("ready", "not_ready"):
                raise ValueError("Unexpected readiness schema")
            dependencies = decoded.get("dependencies", {})
            if not isinstance(dependencies, dict) or set(dependencies) != {"mysql", "redis"} or any(
                value not in ("ok", "unavailable") for value in dependencies.values()
            ):
                raise ValueError("Unexpected dependency readiness schema")
            return {"status_code": response.code, "body": {
                "status": decoded["status"], "dependencies": dependencies,
            }}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"status_code": 0, "body": {}, "error_type": type(exc).__name__}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed read-only lab probes")
    parser.add_argument("check", choices=("mysql", "redis", "ready"))
    action = parser.parse_args().check
    functions = {"mysql": mysql_check, "redis": redis_check, "ready": ready_check}
    # This script is invoked only inside the Linux fixture, where SIGALRM puts
    # a deadline over DNS, connect, authentication and the fixed operation.
    def deadline(_: int, __: Any) -> None:
        raise TimeoutError("Fixed probe deadline expired")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(3)
    try:
        result = functions[action]()
    finally:
        signal.alarm(0)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
