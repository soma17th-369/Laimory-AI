"""관측 payload의 콘텐츠 정책·마스킹·크기 제한 검증."""

from copy import deepcopy

from app.core.observability import (
    ContentCapture,
    REDACTED,
    capture_payload,
    redact_value,
    summarize_content,
)


def test_redaction_masks_nested_secrets_and_personal_text_without_mutation() -> None:
    payload = {
        "authorization": "Bearer abc.def.ghi",
        "nested": {
            "apiKey": "sk-abcdefghijklmnop",
            "error": "연락처 010-1234-5678, mail user@example.com",
        },
    }
    original = deepcopy(payload)

    redacted = redact_value(payload)

    assert payload == original
    assert redacted["authorization"] == REDACTED
    assert redacted["nested"]["apiKey"] == REDACTED
    assert redacted["nested"]["error"] == f"연락처 {REDACTED}, mail {REDACTED}"


def test_sanitized_policy_keeps_content_after_masking() -> None:
    captured = capture_payload(
        {
            "prompt": "민감한 프롬프트 user@example.com",
            "response": {"title": "민감한 응답"},
            "eventCount": 3,
        },
        max_bytes=4096,
    )

    assert captured["eventCount"] == 3
    assert captured["prompt"] == f"민감한 프롬프트 {REDACTED}"
    assert captured["response"] == {"title": "민감한 응답"}
    assert "민감한" in str(captured)
    assert "user@example.com" not in str(captured)


def test_none_policy_replaces_content_keys_with_length_and_hash() -> None:
    captured = capture_payload(
        {
            "prompt": "민감한 프롬프트 user@example.com",
            "response": {"title": "민감한 응답"},
            "eventCount": 3,
        },
        ContentCapture.NONE,
        max_bytes=4096,
    )

    assert captured["eventCount"] == 3
    assert captured["prompt"]["contentCaptured"] is False
    assert captured["response"]["contentCaptured"] is False
    assert len(captured["prompt"]["sha256"]) == 64
    assert "민감한" not in str(captured)
    assert "user@example.com" not in str(captured)


def test_content_summary_is_stable_and_contains_no_content() -> None:
    left = summarize_content({"input": "민감한 원문"})
    right = summarize_content({"input": "민감한 원문"})

    assert left == right
    assert left["contentCaptured"] is False
    assert left["byteLength"] > 0
    assert len(left["sha256"]) == 64
    assert "민감한 원문" not in str(left)


def test_oversized_sanitized_content_keeps_masked_preview() -> None:
    captured = capture_payload({"details": "가" * 1000}, max_bytes=100)

    assert captured["contentCaptured"] is True
    assert captured["truncated"] is True
    assert captured["byteLength"] > 100
    assert captured["storedByteLength"] <= 100
    assert len(captured["sha256"]) == 64
    assert "가" in captured["contentPreview"]


def test_oversized_none_metadata_is_hashed_without_preview() -> None:
    captured = capture_payload(
        {"details": "가" * 1000},
        ContentCapture.NONE,
        max_bytes=100,
    )

    assert captured["metadataCaptured"] is False
    assert captured["truncated"] is True
    assert captured["byteLength"] > 100
    assert len(captured["sha256"]) == 64
    assert "가" not in str(captured)
    assert "contentPreview" not in captured
