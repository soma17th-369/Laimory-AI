"""관측 payload 콘텐츠 정책과 마스킹 검증."""

from copy import deepcopy

from app.core.observability import ContentCapture, REDACTED, capture_payload, redact_value


def test_redaction_masks_nested_secrets_and_personal_text_without_mutation() -> None:
    payload = {
        "authorization": "Bearer abc.def.ghi",
        "nested": {
            "apiKey": "sk-abcdefghijklmnop",
            "message": "연락처 010-1234-5678, mail user@example.com",
            "inputTokens": 42,
        },
        "rows": [{"clientSecret": "hidden", "value": "safe"}],
    }
    original = deepcopy(payload)

    redacted = redact_value(payload)

    assert payload == original
    assert redacted["authorization"] == REDACTED
    assert redacted["nested"]["apiKey"] == REDACTED
    assert redacted["nested"]["message"] == f"연락처 {REDACTED}, mail {REDACTED}"
    assert redacted["nested"]["inputTokens"] == 42
    assert redacted["rows"][0]["clientSecret"] == REDACTED
    assert redacted["rows"][0]["value"] == "safe"


def test_sanitized_capture_keeps_structure_but_removes_secret() -> None:
    captured = capture_payload(
        {"prompt": "use sk-abcdefghijklmnop", "result": [1, 2]},
        ContentCapture.SANITIZED,
    )

    assert captured == {"prompt": f"use {REDACTED}", "result": [1, 2]}


def test_none_capture_keeps_only_length_and_stable_hash() -> None:
    payload = {"prompt": "민감한 원문", "apiKey": "secret"}

    left = capture_payload(payload, ContentCapture.NONE)
    right = capture_payload(payload, ContentCapture.NONE)

    assert left == right
    assert left["contentCaptured"] is False
    assert left["byteLength"] > 0
    assert len(left["sha256"]) == 64
    assert "민감한 원문" not in str(left)
    assert "secret" not in str(left)
