"""New isolated FastAPI fixture: dependencies are checked, never modified."""

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pymysql
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")
SECRET_DIR = Path("/run/secrets")


def read_password(name: str) -> str:
    value = (SECRET_DIR / name).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid runtime credential file")
    return value


def port(name: str, default: str) -> int:
    value = os.environ.get(name, default)
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError(f"Invalid {name}; expected a TCP port")
    return int(value)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    mode = os.environ.get("APP_MODE", "")
    if mode != "test-lab":
        logger.error("Configuration validation failed: APP_MODE must be test-lab")
        raise ValueError("APP_MODE must be test-lab")
    if os.environ.get("MYSQL_HOST") != "mysql":
        raise ValueError("MYSQL_HOST must be mysql")
    if os.environ.get("REDIS_HOST") != "redis":
        raise ValueError("REDIS_HOST must be redis")
    port("MYSQL_PORT", "3306")
    port("REDIS_PORT", "6379")
    read_password("mysql_probe_password")
    read_password("redis_probe_password")
    logger.info("Application configuration validated")
    yield


app = FastAPI(title="Web service readiness", lifespan=lifespan)


@app.get("/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/ready")
def ready() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        with pymysql.connect(
            host="mysql",
            port=port("MYSQL_PORT", "3306"),
            user="probe",
            password=read_password("mysql_probe_password"),
            connect_timeout=2,
            read_timeout=2,
            write_timeout=2,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise ValueError("Unexpected readiness response")
        checks["mysql"] = "ok"
    except (OSError, ValueError, pymysql.MySQLError) as exc:
        # Do not log exception messages: drivers may embed DSNs or credentials.
        code = exc.args[0] if isinstance(exc, pymysql.MySQLError) and exc.args else None
        safe_code = code if isinstance(code, int) else "unavailable"
        logger.error("MySQL readiness failed: error_type=%s code=%s", type(exc).__name__, safe_code)
        checks["mysql"] = "unavailable"
    try:
        with redis.Redis(
            host="redis",
            port=port("REDIS_PORT", "6379"),
            username="probe",
            password=read_password("redis_probe_password"),
            socket_connect_timeout=2,
            socket_timeout=2,
            protocol=2,
        ) as client:
            if client.ping() is not True:
                raise ValueError("Unexpected readiness response")
        checks["redis"] = "ok"
    except (OSError, ValueError, redis.RedisError) as exc:
        logger.error("Redis readiness failed: error_type=%s", type(exc).__name__)
        checks["redis"] = "unavailable"
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if healthy else "not_ready", "dependencies": checks},
        status_code=200 if healthy else 503,
    )
