"""관측 payload의 콘텐츠 정책·마스킹·크기 제한 검증."""

from copy import deepcopy

from app.core.observability import (
    ContentCapture,
    REDACTED,
    capture_payload,
    redact_value,
    summarize_content,
)
from app.core.observability.redaction import capture_external_content


def test_redaction_masks_nested_secrets_and_personal_text_without_mutation() -> None:
    payload = {
        "authorization": "Bearer abc.def.ghi",
        "nested": {
            "apiKey": "sk-abcdefghijklmnop",
            "callbackToken": "callback-token-123",
            "secret_key": "private-value",
            "error": "연락처 010-1234-5678, mail user@example.com",
        },
    }
    original = deepcopy(payload)

    redacted = redact_value(payload)

    assert payload == original
    assert redacted["authorization"] == REDACTED
    assert redacted["nested"]["apiKey"] == REDACTED
    assert redacted["nested"]["callbackToken"] == REDACTED
    assert redacted["nested"]["secret_key"] == REDACTED
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


def test_external_none_policy_blocks_by_default_and_keeps_diagnostics() -> None:
    """외부 전송은 allowlist 다. denylist 에 없는 키도 본문이면 나가지 않는다(이슈 #48)."""

    captured = capture_external_content(
        {
            "ok": True,
            "durationMs": 12.5,
            # `timeline` 은 `_CONTENT_KEYS` 에 없다. denylist 였다면 그대로 나갔다.
            "timeline": {"events": [{"placeLabel": "서울 자택"}]},
        },
        ContentCapture.NONE,
        max_bytes=4096,
    )

    assert captured["ok"] is True
    assert captured["durationMs"] == 12.5
    assert captured["body"]["contentCaptured"] is False
    assert "서울 자택" not in str(captured)


def test_external_capture_does_not_treat_value_as_envelope() -> None:
    """값을 감싸지 않으므로 `input`/`output` 이라는 이름에 걸리지 않는다(이슈 #48)."""

    captured = capture_external_content(
        {"status": "SUCCESS", "errorCode": 1901},
        ContentCapture.NONE,
        max_bytes=4096,
    )

    assert captured == {"status": "SUCCESS", "errorCode": 1901}


def test_external_none_policy_summarizes_non_mapping_values() -> None:
    captured = capture_external_content(
        [{"role": "user", "content": "민감한 프롬프트"}],
        ContentCapture.NONE,
        max_bytes=4096,
    )

    assert captured["contentCaptured"] is False
    assert "민감한 프롬프트" not in str(captured)


def test_external_sanitized_policy_keeps_masked_body() -> None:
    captured = capture_external_content(
        [{"role": "user", "content": "민감한 프롬프트 user@example.com"}],
        ContentCapture.SANITIZED,
        max_bytes=4096,
    )

    assert captured[0]["content"] == f"민감한 프롬프트 {REDACTED}"


def test_external_oversized_payload_keeps_diagnostics_and_truncates_body() -> None:
    captured = capture_external_content(
        {"durationMs": 12.5, "draft": "가" * 1000},
        ContentCapture.SANITIZED,
        max_bytes=100,
    )

    assert captured["durationMs"] == 12.5
    assert captured["body"]["contentCaptured"] is True
    assert captured["body"]["truncated"] is True
    assert captured["body"]["storedByteLength"] <= 100
    assert "가" in captured["body"]["contentPreview"]
