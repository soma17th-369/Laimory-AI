"""캘린더 일정 누락 방지 검증.

캘린더는 사용자가 직접 적어 둔 계획이라 위치와 같은 급의 뼈대다. Timeline Agent 가
통째로 버리면 코드가 되살린다.
"""

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarningSeverity,
)
from app.services.calendar_guard import ensure_calendar_events
from app.services.draft_repair import repair_draft
from tests.fixtures.requests import calendar_item, fixture_raw_id, make_request, stay_item

DAY = "2026-06-20"

CALENDAR_REF = (EventSourceType.CALENDAR, "cal-1")
STAY_REF = (EventSourceType.STAY, "stay-1")


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _request(**overrides):
    defaults = dict(
        calendars=[
            calendar_item(
                1,
                "ASM 프로젝트 MVP 개발",
                start=f"{DAY}T09:00:00",
                end=f"{DAY}T23:00:00",
                raw_id="cal-1",
                location_text="집(경기도 오산시 운암로 90)",
            )
        ],
        stays=[stay_item(2, raw_id="stay-1", start=f"{DAY}T11:00:00", end=f"{DAY}T14:00:00")],
    )
    defaults.update(overrides)
    return make_request(**defaults)


def _event(client_event_id, start, end, *refs, title="이벤트") -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=EventType.REST,
        title=title,
        start_time=_t(start),
        end_time=_t(end),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=st, raw_id=fixture_raw_id(raw_id))
            for st, raw_id in refs
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )


def _titles(draft) -> list[str]:
    return [event.title for event in draft.events]


# --- 되살리기 ------------------------------------------------------------------


def test_a_calendar_event_dropped_by_the_llm_is_restored():
    # 실제 사례: Calendar Agent 는 후보를 만들었는데 Timeline Agent 가 통째로 버렸다.
    draft = _draft(_event("event-001", "11:00", "14:00", STAY_REF, title="집에 머문 오후"))

    ensure_calendar_events(draft, _request())

    restored = next(event for event in draft.events if event.title == "ASM 프로젝트 MVP 개발")
    assert restored.event_type is EventType.CALENDAR_EVENT
    assert restored.start_time.isoformat() == _t("09:00")
    assert restored.end_time.isoformat() == _t("23:00")
    assert restored.place_label == "집"  # locationText 의 라벨만
    assert restored.inference_level is InferenceLevel.DIRECT
    assert restored.confidence == 0.6  # 일정이 있었다 ≠ 그 일을 했다
    assert restored.uncertainty  # 한계를 남긴다
    assert [ref.raw_id for ref in restored.source_refs] == [fixture_raw_id("cal-1")]

    warning = next(w for w in draft.warnings if "되살렸습니다" in w.message)
    assert warning.severity is TimelineWarningSeverity.MEDIUM
    assert "ASM 프로젝트 MVP 개발" in warning.message


def test_a_calendar_event_already_in_the_timeline_is_left_alone():
    draft = _draft(
        _event("event-001", "09:00", "23:00", CALENDAR_REF, STAY_REF, title="집에서 개발한 하루")
    )

    ensure_calendar_events(draft, _request())

    assert _titles(draft) == ["집에서 개발한 하루"]
    assert draft.warnings == []


def test_only_the_missing_calendars_are_restored():
    request = _request(
        calendars=[
            calendar_item(1, "반영된 일정", start=f"{DAY}T09:00:00", end=f"{DAY}T10:00:00", raw_id="cal-1"),
            calendar_item(3, "빠진 일정", start=f"{DAY}T15:00:00", end=f"{DAY}T16:00:00", raw_id="cal-2"),
        ]
    )
    draft = _draft(_event("event-001", "09:00", "10:00", CALENDAR_REF, title="반영된 일정"))

    ensure_calendar_events(draft, request)

    assert _titles(draft) == ["반영된 일정", "빠진 일정"]


def test_a_calendar_without_an_end_becomes_an_instant_event():
    request = _request(
        calendars=[calendar_item(1, "알림 일정", start=f"{DAY}T15:00:00", end=None, raw_id="cal-1")]
    )
    draft = _draft(_event("event-001", "11:00", "14:00", STAY_REF))

    ensure_calendar_events(draft, request)

    restored = next(event for event in draft.events if event.title == "알림 일정")
    assert restored.start_time == restored.end_time


def test_no_calendars_means_nothing_happens():
    draft = _draft(_event("event-001", "11:00", "14:00", STAY_REF, title="집에 머문 오후"))

    ensure_calendar_events(draft, make_request())

    assert _titles(draft) == ["집에 머문 오후"]
    assert draft.warnings == []


# --- 전체 repair 파이프라인 ----------------------------------------------------


def test_the_restored_calendar_event_goes_through_the_rest_of_repair():
    # 되살린 event 도 정렬·id 부여를 똑같이 거친다.
    draft = _draft(_event("event-001", "11:00", "14:00", STAY_REF, title="집에 머문 오후"))

    repair_draft(draft, _request())

    assert _titles(draft) == ["ASM 프로젝트 MVP 개발", "집에 머문 오후"]  # 09:00 이 먼저
    assert [event.client_event_id for event in draft.events] == ["event-001", "event-002"]
