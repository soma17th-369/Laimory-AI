"""window 강제가 조건 없이 항상 도는지 검증 (#67).

예전에는 `resolve_window_bounds` 가 `None` 을 돌려주면 범위 검증이 통째로 꺼졌다.
window 없음·파싱 실패·역전 세 경우가 모두 그 입구였고, `logger.warning` 한 줄만 남긴
채 검증되지 않은 draft 가 저장까지 갔다. 이제는 만들지 않거나 거절한다.
"""

import pytest
from fastapi.testclient import TestClient

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.server import app
from app.services.draft_repair import repair_draft
from app.services.timeline_validator import (
    TimelineViolationCode,
    validate_timeline_for_storage,
)
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

DAY = "2026-06-20"


def _event(client_event_id: str, start: str, end: str) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=EventType.REST,
        title=client_event_id,
        description="d",
        start_time=start,
        end_time=end,
        confidence=0.5,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY, raw_id=fixture_raw_id("stay-1")
            )
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="user-1234",
        date=DAY,
        timezone="Asia/Seoul",
        events=list(events),
        questions=[],
        warnings=[],
    )


# --- repair 경로 -------------------------------------------------------------


def test_events_outside_the_window_are_always_removed() -> None:
    """수면 기록이 없어도 window 밖 event 는 사라진다.

    `enforce_sleep_boundary` 가 하던 "기상 이전 제거"를 window 강제가 대신한다는
    것을 확인하는 자리다.
    """

    request = make_request(
        stays=[stay_item(1, raw_id="stay-1", start=f"{DAY}T09:00:00", end=f"{DAY}T10:00:00")]
    )
    draft = _draft(
        _event("inside", f"{DAY}T09:00:00+09:00", f"{DAY}T10:00:00+09:00"),
        _event("before", "2026-06-19T09:00:00+09:00", "2026-06-19T10:00:00+09:00"),
    )

    repair_draft(draft, request)

    assert [event.title for event in draft.events] == ["inside"]


# --- 저장 경계 ---------------------------------------------------------------


def test_storage_validation_catches_events_outside_the_window() -> None:
    """repair 를 건너뛴 draft 도 저장 직전에 잡힌다 (defense-in-depth)."""

    request = make_request()
    draft = _draft(_event("outside", "2026-06-19T09:00:00+09:00", "2026-06-19T10:00:00+09:00"))

    violations = validate_timeline_for_storage(
        draft, {fixture_raw_id("stay-1")}, request
    )

    assert any("window 밖" in violation for violation in violations)


def test_storage_validation_rejects_hidden_event_types() -> None:
    request = make_request()
    event = _event("sleep", f"{DAY}T01:00:00+09:00", f"{DAY}T06:00:00+09:00")
    event.event_type = EventType.SLEEP

    violations = validate_timeline_for_storage(
        _draft(event), {fixture_raw_id("stay-1")}, request
    )

    assert any("저장하지 않습니다" in violation for violation in violations)


def test_storage_violation_codes_are_stable() -> None:
    """운영 관측이 코드로 집계하므로 값이 흔들리면 안 된다."""

    assert TimelineViolationCode.OUTSIDE_WINDOW.value == "OUTSIDE_WINDOW"
    assert TimelineViolationCode.HIDDEN_EVENT_TYPE.value == "HIDDEN_EVENT_TYPE"


# --- 접수 경계 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-06-21T00:00:00+09:00", "2026-06-20T00:00:00+09:00"),  # 역전
        ("2026-06-20T00:00:00+09:00", "2026-06-20T00:00:00+09:00"),  # 같은 시각
    ],
)
def test_a_reversed_window_is_rejected_at_intake(start: str, end: str) -> None:
    """예전에는 202 로 접수되고 범위 검증만 조용히 꺼졌다."""

    response = TestClient(app).post(
        "/v1/timeline",
        json={
            "taskId": "task-1",
            "taskToken": "token-1",
            "dailyRecordId": 1,
            "window": {"startAt": start, "endAt": end},
        },
    )

    assert response.status_code == 422
