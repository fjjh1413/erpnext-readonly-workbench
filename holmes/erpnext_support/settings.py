"""Explicit deployment settings; no user Holmes configuration is discovered."""

import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from holmes.erpnext_support.security import safe_path


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "erpnext-support"


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("连接地址必须是无路径、凭据和查询参数的 HTTP(S) origin")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("非本机 ERP 接入必须使用 HTTPS")
    _ = parsed.port
    return value.rstrip("/")


@dataclass
class Settings:
    runtime: Path
    allowed_erp_origins: list[str]
    workbench_origins: list[str] = field(default_factory=lambda: ["http://127.0.0.1:8765", "http://localhost:8765"])
    max_rounds: int = 4
    max_tool_calls: int = 30
    total_seconds: int = 90
    tool_timeout_seconds: int = 8
    max_rows: int = 50
    evidence_retention_days: int = 30
    model: str | None = None
    model_api_base: str | None = None
    model_api_key: str | None = field(default=None, repr=False)
    max_model_tokens: int = 2048
    frontend: Path = field(default_factory=lambda: EXAMPLE / "frontend" / "dist")
    database_url: str | None = field(default=None, repr=False)
    broker_url: str | None = field(default=None, repr=False)
    queue_backend: str = "inline"
    task_capacity: int = 100
    task_lease_seconds: int = 60
    metrics_token: str | None = field(default=None, repr=False)
    session_hours: int = 8
    secure_cookies: bool = False

    def __post_init__(self) -> None:
        self.runtime = self.runtime.absolute()
        self.frontend = Path(self.frontend)
        safe_path(self.runtime, self.runtime)
        self.allowed_erp_origins = [origin(v) for v in self.allowed_erp_origins]
        self.workbench_origins = [origin(v) for v in self.workbench_origins]
        if not (1 <= self.max_rounds <= 6 and 5 <= self.max_tool_calls <= 60 and 10 <= self.total_seconds <= 180
                and 1 <= self.tool_timeout_seconds <= 15 and 1 <= self.max_rows <= 100
                and 1 <= self.evidence_retention_days <= 90 and 256 <= self.max_model_tokens <= 4096):
            raise ValueError("诊断预算或保留期限超出允许范围")
        if self.model and not self.model_api_base:
            raise ValueError("模型必须明确配置请求目标 model_api_base")
        if self.model_api_base:
            parsed = urlsplit(self.model_api_base)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("模型请求目标无效")
            if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("外部模型请求必须使用 HTTPS")
        if self.queue_backend not in ("inline", "celery"):
            raise ValueError("后台任务方式无效")
        if not 1 <= self.task_capacity <= 100 or not 30 <= self.task_lease_seconds <= 120 or not 1 <= self.session_hours <= 24:
            raise ValueError("任务容量、租约或登录有效期超出范围")
        if self.database_url:
            if not self.database_url.startswith("postgresql+pg8000://"):
                raise ValueError("企业数据库只支持明确配置的 PostgreSQL pg8000 连接")
            parsed = urlsplit(self.database_url)
            if not parsed.hostname or not parsed.path.strip("/") or parsed.fragment:
                raise ValueError("企业数据库配置无效")
        if self.broker_url:
            parsed = urlsplit(self.broker_url)
            if parsed.scheme not in ("redis", "rediss") or not parsed.hostname or parsed.fragment or parsed.query:
                raise ValueError("消息队列配置无效")
        if self.queue_backend == "celery" and (not self.database_url or not self.broker_url):
            raise ValueError("独立任务模式必须配置 PostgreSQL 与消息队列")
        if self.metrics_token and not 32 <= len(self.metrics_token) <= 256:
            raise ValueError("指标访问凭据长度无效")

    @property
    def storage_target(self) -> Path | str:
        return self.database_url or self.database

    @property
    def database(self) -> Path:
        return safe_path(self.runtime, self.runtime / "workbench.sqlite3")

    def credentials(self, reference: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", reference):
            raise ValueError("凭据别名无效")
        path = safe_path(self.runtime, self.runtime / "credentials" / (reference + ".json"))
        if path.stat().st_size > 4096:
            raise ValueError("凭据文件过大")
        values = json.loads(path.read_text(encoding="utf-8"))
        if set(values) != {"api_key", "api_secret"} or any(not isinstance(v, str) or not 8 <= len(v) <= 256 for v in values.values()):
            raise ValueError("凭据文件字段无效")
        return values

    def collector_credentials(self, reference: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", reference):
            raise ValueError("采集器凭据别名无效")
        path = safe_path(self.runtime, self.runtime / "collectors" / (reference + ".json"))
        if path.stat().st_size > 4096:
            raise ValueError("采集器凭据文件过大")
        values = json.loads(path.read_text(encoding="utf-8"))
        if set(values) != {"base_url", "token"} or not isinstance(values["token"], str) or not 32 <= len(values["token"]) <= 256:
            raise ValueError("采集器凭据字段无效")
        # Initial delivery only supports the scoped local collector. A customer
        # service requires an audited implementation and an explicit extension.
        if values["base_url"] != "http://127.0.0.1:8767":
            raise ValueError("采集器尚未适配此服务端地址")
        return values

    @classmethod
    def load(cls, path: Path) -> "Settings":
        values = json.loads(path.read_text(encoding="utf-8"))
        # Runtime is anchored beside the explicit settings file; never accepted from HTTP.
        values.pop("runtime", None)
        values["runtime"] = path.parent
        values["model_api_key"] = os.environ.get("ERP_SUPPORT_MODEL_API_KEY")
        for key, variable in (("database_url", "ERP_SUPPORT_DATABASE_URL_FILE"),
                              ("broker_url", "ERP_SUPPORT_BROKER_URL_FILE"),
                              ("metrics_token", "ERP_SUPPORT_METRICS_TOKEN_FILE")):
            file_name = os.environ.get(variable)
            if file_name:
                file_path = Path(file_name).absolute()
                safe_path(file_path.parent, file_path)
                if not file_path.is_file() or file_path.stat().st_size > 4096:
                    raise ValueError("私有配置文件无效")
                values[key] = file_path.read_text(encoding="utf-8-sig").strip()
        return cls(**values)


def restrict_runtime(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)
        return
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=10)
    match = re.search(r"S-1-5-[0-9-]+", result.stdout)
    if not match:
        raise ValueError("无法核对本地账户访问权限")
    rights = "(OI)(CI)F" if path.is_dir() else "F"
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{match.group()}:{rights}", "*S-1-5-18:" + rights],
                   capture_output=True, text=True, check=True, timeout=10)


def initialize(runtime: Path, erp_origin: str) -> Path:
    runtime = runtime.absolute()
    safe_path(runtime.parent, runtime)
    runtime.mkdir(parents=True, exist_ok=False)
    restrict_runtime(runtime)
    (runtime / "credentials").mkdir()
    settings = {"allowed_erp_origins": [origin(erp_origin)]}
    with (runtime / "settings.json").open("x", encoding="utf-8") as stream:
        json.dump(settings, stream, indent=2)
    with (runtime / "initial-login.json").open("x", encoding="utf-8") as stream:
        json.dump({"username": "admin", "password": secrets.token_urlsafe(24)}, stream)
    return runtime / "settings.json"
