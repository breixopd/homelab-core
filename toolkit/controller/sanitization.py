"""Bound and redact all controller data before it crosses persistence boundaries."""

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_EVENT_PAYLOAD_BYTES = 32 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_COLLECTION_ITEMS = 256
MAX_VALUE_DEPTH = 8
MAX_VALUE_STRING_LENGTH = 4000

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|pwd|token|secret|authorization|auth[_-]?key|api[_-]?key|"
    r"private[_-]?key|credential|cookie|passphrase|passkey)",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(r"(?i)\bauthorization\s*[:=]\s*bearer\s+[^\s,;]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?ix)\b"
    r"(?P<key>[a-z0-9_.-]*(?:password|passwd|pwd|token|secret|auth[_-]?key|api[_-]?key|"
    r"private[_-]?key|credential|cookie|passphrase|passkey)[a-z0-9_.-]*)"
    r"\s*[:=]\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


class SanitizationError(ValueError):
    pass


def sanitize_message(message: str) -> str:
    redacted = _AUTHORIZATION.sub("Authorization=[REDACTED]", message)
    return _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group('key')}=[REDACTED]", redacted)


def sanitize_object(value: object, *, max_bytes: int) -> dict[str, Any]:
    sanitized = _sanitize_value(value, key="", depth=0)
    if not isinstance(sanitized, dict):
        raise SanitizationError("controller payload must be an object")
    try:
        encoded = json.dumps(
            sanitized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SanitizationError("controller payload must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise SanitizationError("controller payload exceeds its size limit")
    return sanitized


def _sanitize_value(value: object, *, key: str, depth: int) -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth > MAX_VALUE_DEPTH:
        raise SanitizationError("controller payload is nested too deeply")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SanitizationError("controller payload contains a non-finite number")
        return value
    if isinstance(value, str):
        return sanitize_message(value[:MAX_VALUE_STRING_LENGTH])
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise SanitizationError("controller payload contains too many fields")
        if any(not isinstance(item_key, str) for item_key in value):
            raise SanitizationError("controller payload keys must be strings")
        return {
            item_key: _sanitize_value(item_value, key=item_key, depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list | tuple):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise SanitizationError("controller payload contains too many items")
        return [_sanitize_value(item, key="", depth=depth + 1) for item in value]
    raise SanitizationError("controller payload contains an unsupported value")
