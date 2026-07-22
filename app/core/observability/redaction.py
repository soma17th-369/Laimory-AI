"""관측 payload 의 Secret·개인정보 마스킹과 콘텐츠 캡처 정책.

여기서 가리는 것은 API key/Bearer/이메일/전화번호처럼 패턴이 뚜렷한 값뿐이라
완전한 개인정보 제거가 아니다. 주소·캘린더 제목·알림 내용·건강 정보·사진 설명
등은 마스킹으로 완전히 걸러지지 않을 수 있다. 따라서 원문 저장 모드는 두지 않고,
``SANITIZED`` 도 마스킹 뒤 이벤트당 크기 제한을 적용한다.
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
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """정책에 따라 payload 본문(마스킹·크기 제한) 또는 안전한 요약을 반환한다."""

    if max_bytes <= 0:
        raise ValueError("max_bytes 는 1 이상이어야 합니다.")

    redacted = redact_value(payload)
    serialized = _canonical_bytes(redacted)
    byte_length = len(serialized)

    if policy is ContentCapture.SANITIZED:
        if byte_length <= max_bytes:
            return {
                "contentCaptured": True,
                "truncated": False,
                "byteLength": byte_length,
                "content": redacted,
            }

        preview = serialized[:max_bytes].decode("utf-8", errors="ignore")
        return {
            "contentCaptured": True,
            "truncated": True,
            "byteLength": byte_length,
            "storedByteLength": len(preview.encode("utf-8")),
            "contentPreview": preview,
            "sha256": hashlib.sha256(serialized).hexdigest(),
        }

    return {
        "contentCaptured": False,
        "truncated": False,
        "byteLength": byte_length,
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }
