"""Restricted collector client; no daemon access is present in this tool."""

import json
import threading
import time
from typing import Callable
from datetime import datetime
from typing import Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus, Tool, ToolInvokeContext
from holmes.erpnext_support.security import sanitize
from holmes.plugins.toolsets.erpnext_support.models import EmptyParams


SERVICE_CATEGORIES = {"backend": "application", "frontend": "application", "db": "database", "redis-cache": "redis", "redis-queue": "redis"}


class ServiceState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["application", "database", "redis"]
    state: Literal["running", "exited", "created", "dead", "paused", "removing", "restarting"]
    health: Literal["healthy", "unhealthy", "starting", "not_configured"]
    restart_count: int = Field(ge=0, le=1000000)
    ping: Literal["PONG", "failed", "not_checked"]


class SnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    services: dict[str, ServiceState]
    application_logs: str = Field(max_length=12000)

    @model_validator(mode="after")
    def scope(self):
        if set(self.services) != set(SERVICE_CATEGORIES) or any(s.category != SERVICE_CATEGORIES[k] for k, s in self.services.items()):
            raise ValueError("Unexpected collector service scope")
        return self


class SnapshotQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(pattern=r"^holmes-erp-[a-f0-9]{12}$")
    services: list[str] = Field(max_length=5)
    log_tail: int = Field(ge=1, le=50)
    log_window_seconds: int = Field(ge=1, le=900)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["local-restricted-erp-collector"]
    query: SnapshotQuery
    collected_at: datetime
    business_at: None
    payload: SnapshotPayload
    limitations: list[str] = Field(max_length=20)


class RuntimeTool(Tool):
    _credentials: dict = PrivateAttr()
    _authorize: Callable = PrivateAttr()
    _services: list = PrivateAttr()
    _timeout: float = PrivateAttr()

    def __init__(self, credentials: dict, services: list, authorize: Callable, timeout: float):
        super().__init__(name="erp_runtime_health", description="Read only the server-bound collector service health and bounded application logs.", parameters={})
        self._credentials, self._services, self._authorize, self._timeout = credentials, services, authorize, timeout

    def get_parameterized_one_liner(self, params):
        return "读取授权的ERP运行状态"

    def _read(self, cancelled: threading.Event, deadline: float) -> dict:
        session = requests.Session()
        session.trust_env = False
        with session, session.get(self._credentials["base_url"] + "/snapshot", headers={"Authorization": "Bearer " + self._credentials["token"]},
                                  timeout=self._timeout, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ConnectionError()
            content = bytearray()
            for chunk in response.iter_content(1):
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError()
                if len(content) % 1024 == 0 and not self._authorize():
                    raise PermissionError()
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError()
            data = Snapshot.model_validate_json(bytes(content)).model_dump(mode="json")
        if not self._authorize():
            raise PermissionError()
        data["payload"]["services"] = {k: v for k, v in data["payload"]["services"].items() if v["category"] in self._services}
        data["query"]["services"] = list(data["payload"]["services"])
        if "application" not in self._services:
            data["payload"].pop("application_logs", None)
            data["query"].update(log_tail=0, log_window_seconds=0)
        return sanitize(data, [self._credentials["token"]])

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        try:
            EmptyParams.model_validate(params)
            if not self._authorize():
                raise PermissionError()
            deadline = time.monotonic() + self._timeout
            cancelled = threading.Event()
            outcome = []
            def fetch():
                try:
                    outcome.append(self._read(cancelled, deadline))
                except Exception as error:
                    outcome.append(error)
            worker = threading.Thread(target=fetch, daemon=True)
            worker.start()
            worker.join(self._timeout)
            if worker.is_alive():
                cancelled.set()
                raise TimeoutError()
            if isinstance(outcome[0], Exception):
                raise outcome[0]
            if not self._authorize():
                raise PermissionError()
            return StructuredToolResult(status=StructuredToolResultStatus.SUCCESS, data=outcome[0], params={}, invocation=self.name)
        except Exception as error:
            code = "authorization_revoked" if isinstance(error, PermissionError) else "tool_timeout" if isinstance(error, (TimeoutError, requests.Timeout)) else "invalid_tool_parameters" if isinstance(error, ValidationError) else "collector_unavailable"
            return StructuredToolResult(status=StructuredToolResultStatus.ERROR, error=code, params={}, invocation=self.name)


def add_runtime_findings(report: dict) -> None:
    entries = [e for e in report["evidence"] if e["tool_name"] == "erp_runtime_health" and e["status"] == "success"]
    if not entries:
        return
    entry = entries[-1]
    for name, state in entry["payload"].get("services", {}).items():
        report["facts"].append({"text": f"采集时 {name} 容器状态为 {state['state']}，健康检查为 {state['health']}，Redis检查为 {state['ping']}。", "evidence_ids": [entry["id"]]})
        if state["state"] != "running" or state["health"] == "unhealthy" or state["ping"] == "failed":
            report["causes"].append({"code": "runtime_" + state["category"], "status": "candidate",
                                     "text": name + " 运行异常可能影响ERP请求；需要与失败时间及应用请求证据关联，不能单独认定发货根因。", "evidence_ids": [entry["id"]]})
    report["limits"] = [s.replace("运行层采集尚未接入。", "独立运行状态已采集，其业务相关性仍需验证。") for s in report["limits"]]
