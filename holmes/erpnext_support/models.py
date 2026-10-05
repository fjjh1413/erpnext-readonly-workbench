"""Validated workbench requests; these never authorize ERP writes."""

from datetime import datetime
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Any
from holmes.core.models import ToolCallResult
from holmes.erpnext_support.security import sanitize


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(RequestModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class EnvironmentInput(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=250)
    credential_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    collector_ref: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    erpnext_version: str = Field(pattern=r"^15\.\d+\.\d+$")
    frappe_version: str = Field(pattern=r"^15\.\d+\.\d+$")
    installed_apps: list[str] = Field(default_factory=list, max_length=30)
    log_source: Literal["none", "erp-api"] = "none"
    allowed_services: list[Literal["application", "database", "redis"]] = Field(default_factory=list)
    model_consent: bool = False


class TicketInput(RequestModel):
    environment_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    delivery_note: str = Field(min_length=1, max_length=140, pattern=r"^[^\x00-\x1f\x7f/\\?#]+$")
    description: str = Field(min_length=5, max_length=4000)
    occurred_at: datetime
    operator_user: str | None = Field(default=None, max_length=140)
    owner: str | None = Field(default=None, max_length=100)

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("发生时间必须包含时区")
        return value


class TicketEdit(RequestModel):
    owner: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, min_length=5, max_length=4000)
    operator_user: str | None = Field(default=None, max_length=140)


class ActionInput(RequestModel):
    description: str = Field(min_length=5, max_length=4000)


class CloseInput(RequestModel):
    confirmation: str = Field(min_length=5, max_length=4000)
    review_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    manual_override: bool = False


class CaseInput(RequestModel):
    title: str = Field(min_length=3, max_length=140)
    erpnext_version: str = Field(pattern=r"^15\.\d+\.\d+$")
    frappe_version: str = Field(pattern=r"^15\.\d+\.\d+$")
    symptom: str = Field(min_length=5, max_length=4000)
    evidence_requirements: list[str] = Field(min_length=1, max_length=20)
    conditions: list[str] = Field(min_length=1, max_length=20)
    steps: list[str] = Field(min_length=1, max_length=20)
    verification: list[str] = Field(min_length=1, max_length=20)
    source_url: str = Field(pattern=r"^https://[^\s]+$", max_length=500)

    @field_validator("evidence_requirements", "conditions", "steps", "verification")
    @classmethod
    def bounded_text(cls, values: list[str]) -> list[str]:
        if any(not v.strip() or len(v) > 2000 for v in values):
            raise ValueError("案例条目需要非空且长度受限")
        return values


class ApprovalInput(RequestModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=3, max_length=2000)


class UserInput(RequestModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,64}$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "analyst", "reviewer"]
    environment_ids: list[str] = Field(default_factory=list, max_length=30)


class Finding(BaseModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1)


class Cause(Finding):
    code: str
    status: Literal["confirmed", "candidate"]


class Evidence(BaseModel):
    id: str = Field(pattern=r"^E\d{3,}$")
    tool_call_id: str
    tool_name: str
    status: str
    source: str
    query: dict
    collected_at: str | None
    business_at: str | None
    payload: dict
    limitations: list[str]
    elapsed_seconds: float | None


class DiagnosticResult(BaseModel):
    """Validate evidence lineage before a result is persisted or returned."""
    model_config = ConfigDict(extra="allow")
    schema_version: str = "erpnext-support/v1"
    classification: str
    status: str
    mode: Literal["rules_degraded", "holmes_agent"]
    facts: list[Finding]
    causes: list[Cause]
    evidence: list[Evidence]
    missing: list[str]
    limits: list[str]
    manual_steps: list[str]
    verification: list[str]
    tool_calls: list[ToolCallResult]
    metrics: dict[str, Any]

    @model_validator(mode="after")
    def references(self):
        by_call = {c.tool_call_id: c for c in self.tool_calls}
        calls = set(by_call)
        ids = {e.id for e in self.evidence}
        if len(ids) != len(self.evidence) or len(calls) != len(self.tool_calls):
            raise ValueError("Duplicate evidence or call identifiers")
        if any(e.tool_call_id not in calls for e in self.evidence):
            raise ValueError("Evidence must come from an actual recorded tool call")
        if any(e.tool_name != by_call[e.tool_call_id].tool_name or
               e.status != by_call[e.tool_call_id].result.status.value for e in self.evidence):
            raise ValueError("Evidence status and tool identity must match its recorded call")
        for evidence in self.evidence:
            call = by_call[evidence.tool_call_id]
            raw = call.result.data or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {"payload": {}, "limitations": ["工具结果不是有效 JSON，不能作业务证据。"]}
            if not isinstance(raw, dict):
                raw = {"payload": {}, "limitations": ["工具结果结构无效。"]}
            expected = sanitize({"payload": raw.get("payload", {}), "source": raw.get("source", call.tool_name),
                                 "query": raw.get("query", call.result.params or {}),
                                 "collected_at": raw.get("collected_at"), "business_at": raw.get("business_at"),
                                 "limitations": raw.get("limitations", [])})
            if any(getattr(evidence, key) != value for key, value in expected.items()):
                raise ValueError("Evidence contents must match their original tool result")
        if any(not set(f.evidence_ids).issubset(ids) for f in [*self.facts, *self.causes]):
            raise ValueError("Finding references missing evidence")
        successful = {e.id for e in self.evidence if e.status == "success"}
        if any(not set(h["evidence_ids"]).issubset(successful) for h in self.model_extra.get("model_hypotheses", [])):
            raise ValueError("Model hypothesis references unsuccessful evidence")
        return self
