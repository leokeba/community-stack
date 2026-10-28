"""Secret redaction utilities to prevent leaking credentials in output, logs, and diagnostics."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

SENSITIVE_KEY_PATTERNS = (
    "password",
    "secret",
    "token",
    "key",
    "pass",
    "credential",
    "auth",
    "private",
)

REDACTED_PLACEHOLDER = "[REDACTED]"


def is_sensitive_key(key: str) -> bool:
    """Return True if key name matches sensitive patterns."""
    lower_key = key.lower()
    return any(pattern in lower_key for pattern in SENSITIVE_KEY_PATTERNS)


def redact_value(value: Any) -> Any:
    """Redact a single value if non-empty."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return value
    return REDACTED_PLACEHOLDER


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys in dictionaries."""
    redacted: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            redacted[k] = redact_dict(v)
        elif isinstance(v, list):
            redacted[k] = [
                redact_dict(item)
                if isinstance(item, dict)
                else (redact_value(item) if is_sensitive_key(k) else item)
                for item in v
            ]
        elif is_sensitive_key(k):
            redacted[k] = redact_value(v)
        else:
            redacted[k] = v
    return redacted


def redact_text(text: str, known_secrets: Iterable[str]) -> str:
    """Mask known secret strings within freeform text or log lines."""
    result = text
    for secret in known_secrets:
        if secret and len(secret.strip()) >= 4:
            result = result.replace(secret, REDACTED_PLACEHOLDER)

    # Generic patterns: e.g. key=value or TOKEN: value
    result = re.sub(
        r"(?i)(password|secret|token|key|credential|pass)\s*([=:]\s*)(['\"]?)([^'\"\s\n]+)(['\"]?)",
        r"\1\2\3" + REDACTED_PLACEHOLDER + r"\5",
        result,
    )
    return result
