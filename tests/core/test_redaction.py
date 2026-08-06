"""외부 전송 payload의 콘텐츠 정책·마스킹·크기 제한 검증."""

from copy import deepcopy

from app.core.redaction import (
    ContentCapture,
    REDACTED,
    capture_external_content,
    capture_payload,
    redact_value,
    summarize_content,
)


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


# --- User Memory (#65) --------------------------------------------------
#
# User Memory 본문은 SANITIZED 정책에서도 나가지 않는다. 관계·성향·관심사를 압축한
# 프로필이라 마스킹 패턴으로 걸러지지 않는다 — 패턴에 걸릴 만한 형태가 아니라
# 그냥 사람에 대한 문장이다. 그래서 키 이름으로 통째로 접는다.

_MEMORY_BODY = {
    "schemaVersion": "1.0",
    "updatedAt": "2026-08-05T11:00:00+09:00",
    "basicProfile": "경기도에 사는 개발자",
    "relationships": "엄마와 매주 통화합니다",
    "customAttributes": {"반려동물": "고양이 두 마리"},
}


def _has_no_memory_body(value: object) -> bool:
    serialized = str(value)
    return not any(
        body in serialized
        for body in ("경기도", "개발자", "엄마", "고양이", "반려동물")
    )


def test_user_memory_body_is_summarized_in_logs() -> None:
    redacted = redact_value({"userMemory": _MEMORY_BODY})

    summary = redacted["userMemory"]
    assert summary["schemaVersion"] == "1.0"
    assert summary["filledFieldCount"] == 2
    assert summary["customAttributeCount"] == 1
    assert summary["contentCaptured"] is False
    assert _has_no_memory_body(redacted)


def test_user_memory_body_never_reaches_external_capture() -> None:
    captured = capture_external_content(
        {"taskId": "task-1", "request": {"userMemory": _MEMORY_BODY}},
        ContentCapture.SANITIZED,
        max_bytes=4096,
    )

    assert captured["taskId"] == "task-1"
    assert _has_no_memory_body(captured)


def test_user_memory_body_never_reaches_metadata_capture() -> None:
    captured = capture_payload(
        {"userMemory": _MEMORY_BODY},
        ContentCapture.SANITIZED,
        max_bytes=4096,
    )

    assert _has_no_memory_body(captured)


def test_user_memory_of_unknown_shape_is_folded_whole() -> None:
    """dict 가 아니면 형태를 모른다. 요약만 남기고 값은 버린다."""

    redacted = redact_value({"userMemory": "엄마와 매주 통화합니다"})

    assert redacted["userMemory"]["contentCaptured"] is False
    assert _has_no_memory_body(redacted)


def test_user_memory_key_matching_ignores_case_and_separators() -> None:
    redacted = redact_value({"user_memory": _MEMORY_BODY})

    assert _has_no_memory_body(redacted)


# --- 확정된 하루 타임라인 (#64) --------------------------------------------

_DAILY_TIMELINES_BODY = [
    {
        "date": "2026-08-04",
        "events": [
            {
                "eventType": "MEAL",
                "title": "회사 근처에서 점심을 먹었어요",
                "memo": "오랜만에 마음이 놓였어요",
            },
            {"eventType": "SLEEP", "title": "잠들었어요", "memo": None},
        ],
    }
]


def _has_no_daily_timeline_body(value: object) -> bool:
    serialized = str(value)
    return not any(
        body in serialized
        for body in ("회사 근처", "점심", "마음이 놓였", "잠들었")
    )


def test_daily_timeline_body_is_summarized_in_logs() -> None:
    """`memo` 는 사용자가 직접 쓴 글이다. User Memory 와 같은 이유로 개수만 남긴다."""

    redacted = redact_value({"dailyTimelines": _DAILY_TIMELINES_BODY})

    summary = redacted["dailyTimelines"]
    assert summary["dailyTimelineCount"] == 1
    assert summary["eventCount"] == 2
    assert summary["memoCount"] == 1
    assert summary["contentCaptured"] is False
    assert _has_no_daily_timeline_body(redacted)


def test_daily_timeline_body_never_reaches_external_capture() -> None:
    captured = capture_external_content(
        {"taskId": "task-1", "dailyTimelines": _DAILY_TIMELINES_BODY},
        ContentCapture.SANITIZED,
        max_bytes=4096,
    )

    assert captured["taskId"] == "task-1"
    assert _has_no_daily_timeline_body(captured)


def test_daily_timeline_summary_does_not_depend_on_event_field_names() -> None:
    """App Server 가 필드를 더해도 본문이 새면 안 된다."""

    redacted = redact_value(
        {"dailyTimelines": [{"date": "2026-08-04", "events": [{"newField": "점심 이야기"}]}]}
    )

    assert redacted["dailyTimelines"]["eventCount"] == 1
    assert _has_no_daily_timeline_body(redacted)


def test_daily_timelines_of_unknown_shape_are_folded_whole() -> None:
    redacted = redact_value({"dailyTimelines": {"date": "2026-08-04"}})

    assert redacted["dailyTimelines"]["contentCaptured"] is False
