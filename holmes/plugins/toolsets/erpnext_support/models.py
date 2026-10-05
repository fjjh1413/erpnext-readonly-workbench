"""Bounded, server-bound contracts for the ERPNext 15.85.1 support adapter."""

import hashlib
import hmac
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from holmes.plugins.toolsets.docker_readonly.security import redact as redact_secrets

ERP_VERSION = "15.85.1"
FRAPPE_VERSION = "15.87.0"
DOCUMENT_TYPES = (
    "Delivery Note", "Sales Order", "Item", "Warehouse", "Bin", "Stock Ledger Entry",
    "Stock Reservation Entry", "Batch", "Serial and Batch Bundle", "Error Log",
)
CAPABILITIES_METHOD = "holmes_readonly.api.capabilities"
STOCK_METHOD = "holmes_readonly.api.stock_evidence"
SECRET_FIELD = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)")
CONTACT_FIELD = re.compile(r"(?i)(email|mobile|phone|address|contact|customer_name|owner|modified_by)")
EMAIL = re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])")
PHONE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\+?86[ -]?)?1[3-9][0-9]{9}(?![A-Za-z0-9_.-])")
INTERNATIONAL_PHONE = re.compile(r"(?<![A-Za-z0-9_.-])\+[0-9]{1,3}[ -][0-9][0-9 -]{6,12}[0-9](?![0-9])")
IDENTIFIER_FIELDS = frozenset((
    "name", "item_code", "warehouse", "company", "delivery_note", "line_name",
    "against_sales_order", "so_detail", "voucher_no", "voucher_detail_no",
    "batch_no", "serial_and_batch_bundle", "quality_inspection", "return_against",
    "amended_from", "reference_name", "item",
))


class ERPAccessError(RuntimeError):
    def __init__(self, code: str, source: str = "ERPNext"):
        self.code = code
        self.source = source
        super().__init__(f"{code}: {source}")


class ERPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: SecretStr = Field(repr=False)
    api_secret: SecretStr = Field(repr=False)
    expected_erpnext_version: str = ERP_VERSION
    expected_frappe_version: str = FRAPPE_VERSION
    timeout_seconds: float = Field(default=8, ge=1, le=30)
    max_tool_timeout: float = Field(default=30, ge=1, le=60)
    max_rows: int = Field(default=50, strict=True, ge=1, le=100)
    max_lines: int = Field(default=10, strict=True, ge=1, le=10)
    max_response_bytes: int = Field(default=524288, strict=True, ge=4096, le=1048576)
    log_window_seconds: int = Field(default=900, strict=True, ge=1, le=3600)
    site_timezone: str = "Asia/Shanghai"

    @field_validator("base_url")
    @classmethod
    def bound_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("ERP connection must be a server-bound HTTP(S) origin without credentials or path.")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1", "erpnext.localhost"):
            raise ValueError("Plain HTTP is limited to the explicit local test environment.")
        return value.rstrip("/")

    @field_validator("expected_erpnext_version")
    @classmethod
    def only_reviewed_version(cls, value: str) -> str:
        if value != ERP_VERSION:
            raise ValueError("Only ERPNext 15.85.1 has been reviewed by this adapter.")
        return value

    @field_validator("expected_frappe_version")
    @classmethod
    def fixed_frappe_version(cls, value: str) -> str:
        if value != FRAPPE_VERSION:
            raise ValueError("Only the verified Frappe 15.87.0 / ERPNext 15.85.1 pair is supported.")
        return value

    @field_validator("site_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    def sanitize(self, value: Any) -> Any:
        return sanitize(
            value, (self.api_key.get_secret_value(), self.api_secret.get_secret_value()),
            identifier_key=self.api_secret.get_secret_value(),
        )


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LineParams(EmptyParams):
    line_name: str = Field(min_length=1, max_length=140)

    @field_validator("line_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return document_name(value)


def document_name(value: str) -> str:
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 140
        or value != value.strip() or value in (".", "..")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "/\\?#")
    ):
        raise ERPAccessError("invalid_document_name")
    return value


def normalize_posting_time(value: Any) -> str:
    """Compare the same ERP Time value across Frappe JSON and timedelta forms.

    Frappe resource JSON may omit a seconds leading zero. Accept only a bounded
    clock time and preserve all six available microsecond digits without rounding.
    """
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2})(?:\.([0-9]{1,6}))?", value) if isinstance(value, str) else None
    if match is None:
        raise ERPAccessError("invalid_erp_posting_time")
    hour, minute, second = (int(match[index]) for index in (1, 2, 3))
    if hour >= 24 or minute >= 60 or second >= 60:
        raise ERPAccessError("invalid_erp_posting_time")
    fraction = (match[4] or "").ljust(6, "0").rstrip("0")
    return f"{hour:02}:{minute:02}:{second:02}" + ("." + fraction if fraction else "")


def sanitize(value: Any, known_secrets: tuple[str, ...] = (), *, identifier_key: str | None = None) -> Any:
    """Minimize contacts while preserving equality of sensitive business IDs.

    The server-bound credential derives stable opaque aliases for identifier
    fields. These are pseudonyms, not an anonymity guarantee. Generic callers
    without an identifier key continue to receive generic contact redaction.
    """
    def text(content: str) -> str:
        content = redact_secrets(content, known_secrets)
        content = EMAIL.sub("[REDACTED EMAIL]", content)
        content = PHONE.sub("[REDACTED PHONE]", content)
        return INTERNATIONAL_PHONE.sub("[REDACTED PHONE]", content)

    def walk(content: Any, field: str | None = None) -> Any:
        if isinstance(content, dict):
            return {
                str(key): "[REDACTED]"
                if SECRET_FIELD.search(str(key)) or CONTACT_FIELD.search(str(key))
                else walk(item, str(key))
                for key, item in content.items()
            }
        if isinstance(content, (list, tuple)):
            return [walk(item) for item in content]
        if isinstance(content, str):
            safe = text(content)
            if identifier_key and field in IDENTIFIER_FIELDS and safe != content:
                digest = hmac.new(
                    identifier_key.encode("utf-8"),
                    b"erpnext-support-identifier-v1\0" + content.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:32]
                return "RID_" + digest
            return safe
        if isinstance(content, float) and not math.isfinite(content):
            raise ERPAccessError("invalid_numeric_response")
        return content

    return walk(value)


class EvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    query: dict[str, Any]
    collected_at: datetime
    business_at: str | None = None
    payload: dict[str, Any]
    limitations: list[str] = Field(default_factory=list)


class RuleFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    status: str
    text: str
    evidence_ids: list[str]

