"""Safety boundaries for the opt-in, locally generated Docker test lab."""

import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Service = Literal["api", "mysql", "redis"]
LAB_LABEL = "io.holmes.readonly-lab"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
REDACTED = "[REDACTED]"
SECRET_FILES = ("mysql_root_password", "mysql_probe_password", "redis_probe_password")
LAB_RUNTIME_ROOT = Path(__file__).resolve().parents[4] / "examples" / "docker-readonly" / ".runtime"


def reject_linked_path(path: Path, anchor: Path) -> None:
    """Reject both symlinks and Windows directory junctions before resolving."""
    candidate = path.absolute()
    root = anchor.absolute()
    if not candidate.is_relative_to(root):
        raise ScopeError("Path is outside the generated lab runtime directory.")
    for item in (root, *candidate.relative_to(root).parents):
        current = item if item == root else root / item
        if current.is_symlink() or current.is_junction():
            raise ScopeError("Generated lab paths cannot contain symlinks or junctions.")
    if candidate.is_symlink() or candidate.is_junction():
        raise ScopeError("Generated lab paths cannot contain symlinks or junctions.")


def bounded_text(path: Path, limit: int) -> str:
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ScopeError("Generated lab file exceeded its byte limit.")
    return content.decode("utf-8")


class ScopeError(ValueError):
    """A request or target is outside the generated test-lab boundary."""


class BackendError(RuntimeError):
    """A bounded read failed; messages contain no raw command output."""


class LabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(strict=True, pattern=r"^holmes-lab-[a-f0-9]{12}$")
    lab_id: str = Field(strict=True, pattern=r"^[a-f0-9]{32}$")
    secret_dir: Path

    @field_validator("secret_dir")
    @classmethod
    def absolute_secret_directory(cls, value: Path) -> Path:
        return value.expanduser().absolute()

    @model_validator(mode="after")
    def generated_secret_directory(self) -> "LabConfig":
        expected = LAB_RUNTIME_ROOT / self.project / "secrets"
        reject_linked_path(self.secret_dir, LAB_RUNTIME_ROOT.parent.parent)
        if self.secret_dir != expected.absolute():
            raise ScopeError("Secret directory must be .runtime/<generated-project>/secrets in the Docker example.")
        self.secret_dir = self.secret_dir.resolve()
        return self

    @classmethod
    def load(cls, path: str | Path) -> "LabConfig":
        """Load only a generated project file, without including its content in errors."""
        original = Path(path).absolute()
        try:
            reject_linked_path(original, LAB_RUNTIME_ROOT.parent.parent)
            source = original.resolve()
            if source.name != "config.json":
                raise ScopeError("Expected a bounded generated config.json.")
            config = cls.model_validate(json.loads(bounded_text(source, 4096)))
            if source != (LAB_RUNTIME_ROOT / config.project / "config.json").absolute():
                raise ScopeError("Configuration is outside its generated .runtime project directory.")
            if not config.secret_dir.is_relative_to(source.parent):
                raise ScopeError("Secret directory must belong to the generated project.")
            return config
        except ScopeError:
            raise
        except Exception:
            raise ScopeError("Invalid or unreadable generated lab configuration.") from None

    def secrets(self) -> tuple[str, ...]:
        values = []
        for filename in SECRET_FILES:
            path = self.secret_dir / filename
            if not path.exists() and filename == "mysql_root_password":
                continue
            values.append(self.secret(filename))
        return tuple(values)

    def secret(self, filename: str) -> str:
        if filename not in SECRET_FILES:
            raise ScopeError("Secret filename is outside the allowlist.")
        path = self.secret_dir / filename
        try:
            reject_linked_path(path, LAB_RUNTIME_ROOT.parent.parent)
            if not path.resolve().is_relative_to(self.secret_dir):
                raise ScopeError("Secret file must belong to the generated project.")
            value = bounded_text(path, 256).strip()
            if not value or any(char in value for char in "\r\n\0"):
                raise ScopeError("Generated credential is invalid.")
            return value
        except ScopeError:
            raise
        except Exception:
            raise ScopeError("Generated credential file is unavailable.") from None


class ServiceParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    service: Service


class LogsParams(ServiceParams):
    tail: int = Field(default=100, ge=1, le=200)
    since_seconds: int = Field(default=300, ge=1, le=3600)
    contains: str | None = Field(default=None, max_length=100)

    @field_validator("contains")
    @classmethod
    def literal_filter(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("The literal filter must contain printable characters.")
        return value


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def redact(value: Any, known_secrets: Iterable[str] = ()) -> Any:
    """Redact generated credentials and common sensitive log formats recursively."""
    secrets = tuple(secret for secret in known_secrets if secret)
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if re.search(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)", str(key))
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = re.sub(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", rf"\1{REDACTED}@", text)
    text = re.sub(
        r"(?i)(authorization[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:bearer|basic)\s+[^\s,;\"']+|[^\s,;\"']+)",
        rf"\1{REDACTED}", text,
    )
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_-])((?=[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key))[A-Za-z0-9_-]+[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        rf"\1{REDACTED}",
        text,
    )
    return text
