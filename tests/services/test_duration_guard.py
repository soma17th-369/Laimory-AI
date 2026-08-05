"""비캘린더 event 지속시간 상한 검사 (#61).

프롬프트는 지속 구간이 분명하지 않은 event 를 3시간 이내로 만들라고 지시한다. 그 지시를
지켰는지 재는 코드가 없으면 하루가 event 하나로 뭉개져도 결과를 볼 때까지 모른다.

**재기만 한다.** 어디서 끊을지는 의미 판단이라 코드가 정하지 않고 Repair 가
`OVEREXTENDED_EVENT` 로 처리한다. 그래서 이 테스트는 event 가 잘리거나 나뉘지 않는 것도
함께 확인한다.
"""

import pytest

from app.schemas import TimelineDraft, TimelineWarningSeverity
from app.services.duration_guard import verify_event_duration
from tests.fixtures.requests import fixture_raw_id

STAY_1 = fixture_raw_id("stay-1")


def _draft(events: list[dict]) -> TimelineDraft:
    return TimelineDraft.model_validate(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": events,
            "questions": [],
            "warnings": [],
        }
    )


def _event(
    *,
    event_type: str = "WORK",
    start: str = "09:00:00",
    end: str = "12:00:00",
    title: str = "마포에서 보낸 하루",
    source_type: str = "STAY",
) -> dict:
    return {
        "clientEventId": "event-001",
        "eventType": event_type,
        "title": title,
        "description": "오전부터 마포에 머물렀어요.",
        "startTime": f"2026-06-20T{start}+09:00",
        "endTime": f"2026-06-20T{end}+09:00",
        "confidence": 0.7,
        "inferenceLevel": "INFERRED",
        "sourceRefs": [{"sourceType": source_type, "rawId": STAY_1, "reason": "체류"}],
    }


def _duration_warnings(draft: TimelineDraft) -> list:
    return [w for w in draft.warnings if w.warning_id.startswith("warning-event-duration-")]


def test_exactly_three_hours_is_not_warned():
    draft = _draft([_event(start="09:00:00", end="12:00:00")])

    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


def test_over_three_hours_is_warned():
    draft = _draft([_event(start="09:00:00", end="12:01:00")])

    verify_event_duration(draft)

    warnings = _duration_warnings(draft)
    assert len(warnings) == 1
    assert warnings[0].severity is TimelineWarningSeverity.LOW
    assert "마포에서 보낸 하루" in warnings[0].message
    assert [ref.raw_id for ref in warnings[0].source_refs] == [STAY_1]


def test_guard_does_not_modify_event_times():
    # 자르거나 나누지 않는다. 분할 판단은 Repair 몫이다.
    draft = _draft([_event(start="09:00:00", end="21:00:00")])
    before = (draft.events[0].start_time, draft.events[0].end_time)

    verify_event_duration(draft)

    assert (draft.events[0].start_time, draft.events[0].end_time) == before
    assert len(draft.events) == 1


@pytest.mark.parametrize("event_type", ["MOVEMENT", "MEAL"])
def test_exempt_event_types_are_not_warned(event_type):
    """근거 종류와 무관하게 상한을 적용하지 않는 종류.

    `MOVEMENT` 는 실제 이동 구간을 통째로 품고, `MEAL` 은 `meal_guard` 가 20~60분으로
    이미 전담하므로 여기서 두 번 경고하지 않는다.
    """

    draft = _draft([_event(event_type=event_type, start="09:00:00", end="21:00:00")])

    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


@pytest.mark.parametrize("source_type", ["CALENDAR", "SLEEP"])
def test_span_evidence_exempts_by_actual_source_not_label(source_type):
    """지속 구간을 직접 제공하는 근거를 인용하면 면제한다(#67)."""

    draft = _draft(
        [_event(start="09:00:00", end="21:00:00", source_type=source_type)]
    )

    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


@pytest.mark.parametrize("event_type", ["CALENDAR_EVENT", "SLEEP"])
def test_a_label_without_span_evidence_is_still_warned(event_type):
    """라벨만 캘린더·수면인 event 는 면제하지 않는다(#67).

    LLM 이 `eventType` 을 그렇게 적었다는 것과 그 event 가 정말 일정·수면 기록을
    근거로 든다는 것은 다르다. 라벨만으로 빼 주면 근거 없는 12시간짜리 event 가
    조용히 통과한다.
    """

    draft = _draft(
        [_event(event_type=event_type, start="09:00:00", end="21:00:00")]
    )

    verify_event_duration(draft)

    assert len(_duration_warnings(draft)) == 1


def test_repeated_runs_do_not_accumulate():
    draft = _draft([_event(start="09:00:00", end="21:00:00")])

    verify_event_duration(draft)
    verify_event_duration(draft)

    assert len(_duration_warnings(draft)) == 1


def test_warning_disappears_after_repair_shortens_event():
    draft = _draft([_event(start="09:00:00", end="21:00:00")])
    verify_event_duration(draft)
    assert _duration_warnings(draft)

    # Repair 가 update_event 로 시간을 줄인 상황.
    draft.events[0].end_time = draft.events[0].start_time.replace(hour=11)
    verify_event_duration(draft)

    assert _duration_warnings(draft) == []
