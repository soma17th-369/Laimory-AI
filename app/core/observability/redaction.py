"""관측 payload의 민감정보 제거와 콘텐츠 캡처 정책.

``SANITIZED`` 정책에서는 입력·프롬프트·응답·draft·도구 인자 같은 실행 본문을
Secret과 식별 가능한 개인정보 패턴을 마스킹한 뒤 저장한다. ``NONE`` 정책에서는
본문 키를 길이와 SHA-256으로 치환한다. 어느 정책이든 이벤트당 크기 제한을 적용하며,
마스킹하지 않은 원문 저장 모드는 제공하지 않는다.
"""

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
    "awsaccesskeyid",
    "awssecretaccesskey",
    "callbacktoken",
    "password",
    "passwd",
    "cookie",
    "setcookie",
    "secret",
    "secretkey",
    "clientsecret",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "sessiontoken",
}
# 이 키들의 값은 마스킹 후에도 저장하지 않는다. 키 이름은 비교 전에 영숫자 소문자로
# 정규화하므로 system_prompt, source-items 같은 표기도 함께 차단된다.
_CONTENT_KEYS = {
    "args",
    "body",
    "call",
    "content",
    "contentpreview",
    "draft",
    "fields",
    "input",
    "inputdata",
    "message",
    "options",
    "output",
    "plan",
    "prompt",
    "request",
    "response",
    "result",
    "sourceitems",
    "system",
    "systemprompt",
    "text",
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
    (
        re.compile(
            r"(?<!\d)(?:\+82[- ]?1[016789]|01[016789])[- ]?\d{3,4}[- ]?\d{4}(?!\d)"
        ),
        REDACTED,
    ),
)


def _normalized_key(key: Any) -> str:
    return _KEY_NORMALIZER.sub("", str(key).lower())


def _is_sensitive_key(key: Any) -> bool:
    return _normalized_key(key) in _SENSITIVE_KEYS


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


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def summarize_content(value: Any) -> dict[str, Any]:
    """본문을 저장하지 않고 byte 길이와 안정적인 해시만 반환한다."""

    serialized = _canonical_bytes(value)
    return {
        "contentCaptured": False,
        "byteLength": len(serialized),
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }


def _suppress_content(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                summarize_content(item)
                if _normalized_key(key) in _CONTENT_KEYS
                else _suppress_content(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_suppress_content(item) for item in value]
    return value


def capture_payload(
    payload: Mapping[str, Any],
    policy: ContentCapture = ContentCapture.SANITIZED,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """정책에 따라 마스킹한 본문 또는 본문 요약을 반환한다."""

    if max_bytes <= 0:
        raise ValueError("max_bytes 는 1 이상이어야 합니다.")

    safe = redact_value(
        payload if policy is ContentCapture.SANITIZED else _suppress_content(payload)
    )
    serialized = _canonical_bytes(safe)
    if len(serialized) <= max_bytes:
        return dict(safe)

    if policy is ContentCapture.SANITIZED:
        preview = serialized[:max_bytes].decode("utf-8", errors="ignore")
        return {
            "contentCaptured": True,
            "truncated": True,
            "byteLength": len(serialized),
            "storedByteLength": len(preview.encode("utf-8")),
            "contentPreview": preview,
            "sha256": hashlib.sha256(serialized).hexdigest(),
        }

    return {
        "metadataCaptured": False,
        "truncated": True,
        "byteLength": len(serialized),
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }
