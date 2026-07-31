"""관측 payload의 민감정보 제거와 콘텐츠 캡처 정책.

``SANITIZED`` 정책에서는 입력·프롬프트·응답·draft·도구 인자 같은 실행 본문을
Secret과 식별 가능한 개인정보 패턴을 마스킹한 뒤 저장한다. ``NONE`` 정책에서는
본문을 길이와 SHA-256으로 치환한다. 어느 정책이든 이벤트당 크기 제한을 적용하며,
마스킹하지 않은 원문 저장 모드는 제공하지 않는다.

본문을 가리는 방식은 두 가지이고, 쓰는 곳이 다르다(이슈 #48).

- ``capture_payload``: Elasticsearch 관측용. payload 는 호출부가 조립한 dict 이고
  본문 키가 그 안에 들어 있으므로, ``_CONTENT_KEYS`` denylist 로 그 키만 요약한다.
- ``capture_external_content``: Langfuse 같은 외부 전송용. 값이 dict 가 아닐 수도 있고
  키 이름을 통제할 수 없으므로 ``_DIAGNOSTIC_KEYS`` allowlist 로 **기본 차단**한다.
  denylist 는 목록에 없는 새 키가 조용히 나가므로 외부 경로에는 쓰지 않는다.
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
    "plans",
    "prompt",
    "request",
    "response",
    "result",
    "sourceitems",
    "system",
    "systemprompt",
    "text",
}
# 외부 전송 경로에서 콘텐츠 정책과 무관하게 남기는 진단 지표(이슈 #48). 값이 숫자·열거형·
# 식별자라 본문이 아니고, 이게 없으면 NONE 정책에서 어느 단계가 얼마나 걸렸는지, 무엇이
# 실패했는지조차 알 수 없다. 여기 없는 키는 전부 본문으로 보고 요약으로 접는다.
_DIAGNOSTIC_KEYS = {
    "agent",
    "agentcount",
    "dailyrecordid",
    "durationms",
    "errorcode",
    "errortype",
    "generationcount",
    "iteration",
    "maxiterations",
    "model",
    "ok",
    "provider",
    "sequence",
    "stage",
    "status",
    "taskid",
    "tokenusage",
    "tool",
    "usagedetails",
}
# 접어 넣은 본문이 들어가는 자리. 진단 지표와 같은 depth 에 두어 화면에서 바로 구분된다.
_SUPPRESSED_BODY_KEY = "body"
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


def _split_diagnostics(value: Any) -> tuple[dict[str, Any], Any]:
    """최상위에서 진단 지표와 본문을 나눈다. Mapping 이 아니면 전부 본문이다."""

    if not isinstance(value, Mapping):
        return {}, value

    diagnostics: dict[str, Any] = {}
    body: dict[str, Any] = {}
    for key, item in value.items():
        if _normalized_key(key) in _DIAGNOSTIC_KEYS:
            diagnostics[key] = item
        else:
            body[key] = item
    return diagnostics, body


def suppress_body(value: Any) -> Any:
    """진단 지표만 남기고 나머지는 요약 하나로 접는다(기본 차단).

    ``_suppress_content`` 와 달리 키 목록에 걸린 값만 지우는 게 아니라, 목록에 없는
    값을 전부 지운다. 외부로 나가는 경로에서는 키가 새로 생겨도 본문이 새면 안 된다.

    진단 지표는 subtree 를 통째로 남긴다. ``tokenUsage`` 처럼 중첩 dict 인 지표가 있어서
    안쪽을 다시 걸러내면 ``generationCount``/``inputTokens`` 같은 집계가 사라진다.
    """

    if not isinstance(value, Mapping):
        return summarize_content(value)

    diagnostics, body = _split_diagnostics(value)
    if body:
        diagnostics[_SUPPRESSED_BODY_KEY] = summarize_content(body)
    return diagnostics


def capture_external_content(
    value: Any,
    policy: ContentCapture = ContentCapture.NONE,
    *,
    max_bytes: int,
) -> Any:
    """외부 전송 경로(Langfuse)로 나갈 값 하나를 정책에 따라 캡처한다.

    ``capture_payload`` 와 달리 dict 봉투를 요구하지 않는다. 값을 ``{"input": ...}``
    처럼 감싸면 봉투 키 이름이 그대로 정책 판정에 걸려, 호출부가 넣지도 않은 이유로
    payload 전체가 사라진다(이슈 #48).

    크기 제한에 걸려도 진단 지표는 남기고 본문만 줄인다.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes 는 1 이상이어야 합니다.")

    safe = redact_value(
        value if policy is ContentCapture.SANITIZED else suppress_body(value)
    )
    if len(_canonical_bytes(safe)) <= max_bytes:
        return safe

    diagnostics, body = _split_diagnostics(safe)
    serialized = _canonical_bytes(body)
    summary: dict[str, Any] = {
        "contentCaptured": policy is ContentCapture.SANITIZED,
        "truncated": True,
        "byteLength": len(serialized),
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }
    if policy is ContentCapture.SANITIZED:
        preview = serialized[:max_bytes].decode("utf-8", errors="ignore")
        summary["storedByteLength"] = len(preview.encode("utf-8"))
        summary["contentPreview"] = preview
    return {**diagnostics, _SUPPRESSED_BODY_KEY: summary}


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
