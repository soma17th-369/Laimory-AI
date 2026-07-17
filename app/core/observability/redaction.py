"""관측 payload의 Secret·개인정보 마스킹과 콘텐츠 캡처 정책."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.core.observability.models import ContentCapture

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "password",
    "passwd",
    "cookie",
    "setcookie",
    "secret",
    "clientsecret",
    "accesstoken",
    "refreshtoken",
    "idtoken",
}
_KEY_NORMALIZER = re.compile(r"[^a-z0-9]")
_TEXT_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), REDACTED),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"), REDACTED),
    (
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
        ),
        REDACTED,
    ),
    (re.compile(r"(?<!\d)(?:\+82[- ]?1[016789]|01[016789])[- ]?\d{3,4}[- ]?\d{4}(?!\d)"), REDACTED),
)


def _is_sensitive_key(key: Any) -> bool:
    normalized = _KEY_NORMALIZER.sub("", str(key).lower())
    return normalized in _SENSITIVE_KEYS


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _TEXT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_value(value: Any) -> Any:
    """입력 객체를 변경하지 않고 중첩된 민감 값을 마스킹한다."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    return value


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def capture_payload(
    payload: Mapping[str, Any],
    policy: ContentCapture,
) -> dict[str, Any]:
    """정책에 따라 payload 본문 또는 안전한 요약을 반환한다."""

    if policy is ContentCapture.SANITIZED:
        return redact_value(payload)

    serialized = _canonical_bytes(payload)
    return {
        "contentCaptured": False,
        "byteLength": len(serialized),
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }
