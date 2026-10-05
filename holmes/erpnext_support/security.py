"""Server-side credential, session and evidence boundaries."""

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

from holmes.plugins.toolsets.docker_readonly.security import redact


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = encoded.split(":", 1)
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_path(root: Path, candidate: Path) -> Path:
    root, candidate = root.absolute(), candidate.absolute()
    if not candidate.is_relative_to(root) or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("凭据路径不在授权目录内")
    for current in (candidate, *candidate.parents):
        if current.is_symlink() or current.is_junction():
            raise ValueError("拒绝链接或目录联接")
    return candidate


def sanitize(value: Any, known_secrets: list[str] | None = None) -> Any:
    value = redact(value, known_secrets or [])
    if isinstance(value, str):
        value = re.sub(r"(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])", "[EMAIL]", value, flags=re.I)
        value = re.sub(r"(?<![A-Za-z0-9_.-])(?:\+?86[- ]?)?1[3-9]\d{9}(?![A-Za-z0-9_.-])", "[PHONE]", value)
        return value
    if isinstance(value, list):
        return [sanitize(v, known_secrets) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, known_secrets) for k, v in value.items()}
    return value
