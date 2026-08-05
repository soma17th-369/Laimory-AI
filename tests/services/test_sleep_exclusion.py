"""수면·기상 비노출 경계 검증 (#67).

수면 기록을 믿을 수 없다고 판단해 사용자 결과에서 뺐다. 화면에서만 감추는 것이
아니라 **다른 event 의 근거로도 쓰이지 않아야** 한다는 것이 이 정책의 핵심이다.
"""

import pytest

from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.services.sleep_exclusion import (
    HIDDEN_EVENT_TYPES,
    apply_sleep_exclusion,
    is_sleep_calendar_title,
    sleep_excluded_raw_ids,
)
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    sleep_item,
    steps_item,
)

DAY = "2026-06-20"


def _candidate(event_type: EventType, *refs: tuple[EventSourceType, str]):
    return AiEventCandidate(
        event_type=event_type,
        time_range=CandidateTimeRange(
            start_time=f"{DAY}T01:10:00+09:00", end_time=f"{DAY}T06:50:00+09:00"
        ),
        title="t",
        description="d",
        source_refs=[
            SourceRef(source_type=source_type, raw_id=fixture_raw_id(raw_id))
            for source_type, raw_id in refs
        ],
        confidence=0.5,
        inference_level=InferenceLevel.EVIDENCE_BASED,
    )


def _event(client_event_id: str, event_type: EventType, *refs):
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=client_event_id,
        description="d",
        start_time=f"{DAY}T09:00:00+09:00",
        end_time=f"{DAY}T10:00:00+09:00",
        confidence=0.5,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=source_type, raw_id=fixture_raw_id(raw_id))
            for source_type, raw_id in refs
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


# --- 제외 집합 계산 ----------------------------------------------------------


def test_sleep_candidate_evidence_is_excluded() -> None:
    result = AgentEventResult(
        candidates=[_candidate(EventType.SLEEP, (EventSourceType.SLEEP, "sleep-1"))]
    )

    excluded = sleep_excluded_raw_ids([result], make_request())

    assert fixture_raw_id("sleep-1") in excluded


def test_non_sleep_evidence_of_a_sleep_candidate_survives() -> None:
    """수면 후보가 인용한 STAY 까지 빼면 멀쩡한 낮 event 가 함께 사라진다."""

    result = AgentEventResult(
        candidates=[
            _candidate(
                EventType.SLEEP,
                (EventSourceType.SLEEP, "sleep-1"),
                (EventSourceType.STAY, "stay-1"),
            )
        ]
    )

    excluded = sleep_excluded_raw_ids([result], make_request())

    assert fixture_raw_id("stay-1") not in excluded


def test_health_sleep_records_are_excluded_without_agent_results() -> None:
    request = make_request(
        healths=[sleep_item(1, f"{DAY}T01:10:00", f"{DAY}T06:50:00", 340, raw_id="sleep-1")]
    )

    assert fixture_raw_id("sleep-1") in sleep_excluded_raw_ids([], request)


def test_step_counts_are_not_excluded() -> None:
    """걸음 수는 하루 맥락 근거로 계속 쓴다."""

    request = make_request(healths=[steps_item(1, 9785, raw_id="steps-1")])

    assert fixture_raw_id("steps-1") not in sleep_excluded_raw_ids([], request)


def test_a_sleep_titled_calendar_is_excluded_as_a_fallback() -> None:
    """Agent 가 분류에 실패해도 `수면 zzz` 일정이 새어 나가지 않게 한다."""

    request = make_request(
        calendars=[calendar_item(1, "수면 zzz", raw_id="cal-sleep")]
    )

    assert fixture_raw_id("cal-sleep") in sleep_excluded_raw_ids([], request)


@pytest.mark.parametrize("title", ["수면 zzz", "취침", "낮잠", "Sleep", "ZZZ"])
def test_sleep_titles_are_detected(title: str) -> None:
    assert is_sleep_calendar_title(title)


@pytest.mark.parametrize("title", ["잠실 미팅", "잠깐 산책", "기상청 브리핑", "팀 회의", ""])
def test_ordinary_titles_are_not_mistaken_for_sleep(title: str) -> None:
    """`잠실`·`잠깐`·`기상청` 때문에 멀쩡한 일정이 사라지면 안 된다."""

    assert not is_sleep_calendar_title(title)


# --- draft 적용 --------------------------------------------------------------


def test_sleep_events_are_hidden() -> None:
    draft = _draft(
        _event("sleep", EventType.SLEEP, (EventSourceType.SLEEP, "sleep-1")),
        _event("work", EventType.WORK, (EventSourceType.STAY, "stay-1")),
    )

    apply_sleep_exclusion(draft, frozenset({fixture_raw_id("sleep-1")}))

    assert [event.client_event_id for event in draft.events] == ["work"]
    assert all(event.event_type not in HIDDEN_EVENT_TYPES for event in draft.events)


def test_excluded_refs_are_stripped_but_the_event_survives() -> None:
    draft = _draft(
        _event(
            "morning",
            EventType.SOCIAL,
            (EventSourceType.SLEEP, "sleep-1"),
            (EventSourceType.NOTIFICATION, "notif-1"),
        )
    )

    apply_sleep_exclusion(draft, frozenset({fixture_raw_id("sleep-1")}))

    [event] = draft.events
    assert [ref.source_type for ref in event.source_refs] == [
        EventSourceType.NOTIFICATION
    ]


def test_an_event_with_only_excluded_evidence_is_removed() -> None:
    draft = _draft(_event("only", EventType.REST, (EventSourceType.SLEEP, "sleep-1")))

    apply_sleep_exclusion(draft, frozenset({fixture_raw_id("sleep-1")}))

    assert draft.events == []
    assert any("남은 근거가 없어" in warning.message for warning in draft.warnings)


def test_nothing_happens_without_sleep_data() -> None:
    draft = _draft(_event("work", EventType.WORK, (EventSourceType.STAY, "stay-1")))

    apply_sleep_exclusion(draft, frozenset())

    assert len(draft.events) == 1
    assert draft.warnings == []
