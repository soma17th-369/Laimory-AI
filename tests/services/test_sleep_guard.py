"""수면 경계 강제 검증.

자는 동안에는 아무 일도 일어나지 않는다. 알림은 자는 사람에게도 도착하지만, 그 알림이
event 의 시작 시각이 될 수는 없다. 수면을 제외한 하루의 모든 event 는 기상 이후다.
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
from app.services.sleep_guard import enforce_sleep_boundary, sleep_spans, wake_time
from app.services.validator import resolve_timezone
from tests.fixtures.requests import fixture_raw_id, make_request, sleep_item

DAY = "2026-06-20"

SLEEP_START = f"{DAY}T01:10:00"
WAKE = f"{DAY}T06:50:00"

STAY_REF = (EventSourceType.STAY, "stay-1")
NOTIFICATION_REF = (EventSourceType.NOTIFICATION, "notif-1")
SLEEP_REF = (EventSourceType.SLEEP, "sleep-1")


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _request(healths=None):
    if healths is None:
        healths = [sleep_item(1, SLEEP_START, WAKE, 340, raw_id="sleep-1")]
    return make_request(healths=healths)


def _event(
    client_event_id,
    start,
    end,
    *refs,
    event_type=EventType.REST,
    title=None,
) -> TimelineEventDraft:
    used = refs or (STAY_REF,)
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title or f"{event_type.value} {start}",
        start_time=_t(start),
        end_time=_t(end),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=st, raw_id=fixture_raw_id(raw_id))
            for st, raw_id in used
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u",
        date=DAY,
        timezone="Asia/Seoul",
        events=list(events),
    )


def _titles(draft) -> list[str]:
    return [event.title for event in draft.events]


# --- 수면 구간 읽기 -----------------------------------------------------------


def test_wake_time_is_the_end_of_the_sleep_record():
    tz = resolve_timezone("Asia/Seoul")

    assert wake_time(_request(), tz).isoformat() == _t("06:50")


def test_no_sleep_record_means_no_wake_time():
    tz = resolve_timezone("Asia/Seoul")

    assert wake_time(make_request(), tz) is None
    assert sleep_spans(make_request(), tz) == []


def test_a_sleep_record_without_an_end_is_unusable():
    tz = resolve_timezone("Asia/Seoul")
    request = make_request(healths=[sleep_item(1, SLEEP_START, None, 340, raw_id="sleep-1")])

    assert sleep_spans(request, tz) == []


# --- 제거와 클램프 ------------------------------------------------------------


def test_an_event_entirely_inside_sleep_is_removed():
    draft = _draft(_event("event-001", "03:00", "04:00", title="새벽의 유령"))

    enforce_sleep_boundary(draft, _request())

    assert _titles(draft) == []
    warning = next(w for w in draft.warnings if "수면 중이라" in w.message)
    assert warning.severity is TimelineWarningSeverity.MEDIUM
    assert "새벽의 유령" in warning.message


def test_an_event_straddling_the_wake_time_is_clamped_to_the_wake_time():
    # 실제 사례: 새벽 2시 32분에 온 카톡 하나가 아침 event 를 새벽으로 끌어내렸다.
    draft = _draft(
        _event("event-001", "02:32", "07:52", NOTIFICATION_REF, title="아침부터 이어진 연락")
    )

    enforce_sleep_boundary(draft, _request())

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("06:50")
    assert event.end_time.isoformat() == _t("07:52")
    assert any("수면 중에 도착한 근거" in note for note in event.uncertainty)
    warning = next(w for w in draft.warnings if "수면 구간에 걸친 event" in w.message)
    assert warning.severity is TimelineWarningSeverity.LOW


def test_an_event_before_sleep_starts_is_removed_too():
    # 사용자 확정: "수면을 제외한 모든 event 는 기상 후에 일어난다." 잠들기 직전의
    # 체류도 기상 이전이므로 남기지 않는다.
    draft = _draft(_event("event-001", "00:35", "00:53", title="잠들기 전 체류"))

    enforce_sleep_boundary(draft, _request())

    assert _titles(draft) == []


def test_an_event_after_waking_is_untouched():
    draft = _draft(_event("event-001", "09:00", "10:00", title="오전 작업"))

    enforce_sleep_boundary(draft, _request())

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("09:00")
    assert event.end_time.isoformat() == _t("10:00")
    assert draft.warnings == []


def test_an_instant_event_during_sleep_is_removed_but_one_after_waking_survives():
    draft = _draft(
        _event("event-001", "03:00", "03:00", event_type=EventType.PHOTO_MOMENT, title="새벽 사진"),
        _event("event-002", "13:00", "13:00", event_type=EventType.PHOTO_MOMENT, title="점심 사진"),
    )

    enforce_sleep_boundary(draft, _request())

    assert _titles(draft) == ["점심 사진"]


# --- 면제 --------------------------------------------------------------------


def test_the_sleep_event_itself_survives():
    draft = _draft(_event("event-001", "01:10", "06:50", SLEEP_REF, event_type=EventType.SLEEP, title="수면"))

    enforce_sleep_boundary(draft, _request())

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("01:10")
    assert event.end_time.isoformat() == _t("06:50")


def test_wake_up_is_snapped_to_the_end_of_sleep():
    # 기상은 수면이 끝난 그 시점이다. LLM 이 엉뚱한 시각을 적어도 코드가 확정한다.
    draft = _draft(_event("event-001", "07:30", "07:30", SLEEP_REF, event_type=EventType.WAKE_UP))

    enforce_sleep_boundary(draft, _request())

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("06:50")
    assert event.end_time.isoformat() == _t("06:50")


# --- 경계 조건 ---------------------------------------------------------------


def test_no_sleep_record_means_nothing_is_enforced():
    draft = _draft(_event("event-001", "03:00", "04:00", title="새벽 활동"))

    enforce_sleep_boundary(draft, make_request())

    assert _titles(draft) == ["새벽 활동"]  # 기상 시각을 모르면 강제할 근거가 없다


def test_a_nap_does_not_erase_the_morning():
    # 기상 시각은 **가장 이른** 수면 종료다. 낮잠 기록이 섞여도 아침이 지워지면 안 된다.
    request = _request(
        healths=[
            sleep_item(1, SLEEP_START, WAKE, 340, raw_id="sleep-1"),
            sleep_item(2, f"{DAY}T14:00:00", f"{DAY}T15:00:00", 60, raw_id="sleep-2"),
        ]
    )
    draft = _draft(
        _event("event-001", "09:00", "10:00", title="오전 작업"),
        _event("event-002", "14:10", "14:40", title="낮잠 중의 유령"),
    )

    enforce_sleep_boundary(draft, request)

    assert _titles(draft) == ["오전 작업"]  # 낮잠 구간만 금지된다


def test_removing_an_event_keeps_the_remaining_ids():
    """번호는 확정 pass 끝에서만 준다(#144). Repair 가 이 guard 를 도구로 부른 뒤 같은
    계획의 다음 호출이 다른 event 를 가리키면 안 된다."""

    draft = _draft(
        _event("event-001", "03:00", "04:00", title="새벽의 유령"),
        _event("event-002", "09:00", "10:00", title="오전 작업"),
    )

    enforce_sleep_boundary(draft, _request())

    assert [event.client_event_id for event in draft.events] == ["event-002"]


# --- 수면과 겹친 event 제거 (#134) ----------------------------------------------
#
# 캘린더 수면으로 만든 `SLEEP` 은 `enforce_sleep_boundary`(HEALTH 만 읽는다)의 경계 밖이다.
# 밤사이 체류가 수면보다 길면 Timeline 이 체류 전체를 `REST` 로 남기고 그 안에 `SLEEP` 을
# 얹었고, 프롬프트로는 안정적으로 막지 못했다. 겹치면 자르지 않고 통째로 지운다.

from app.services.draft_repair import repair_draft  # noqa: E402
from app.services.sleep_guard import remove_events_overlapping_sleep  # noqa: E402
from tests.fixtures.requests import calendar_item, stay_item  # noqa: E402

CALENDAR_SLEEP_REF = (EventSourceType.CALENDAR, "calendar-sleep")
PHOTO_REF = (EventSourceType.PHOTO, "photo-1")
OTHER_CALENDAR_REF = (EventSourceType.CALENDAR, "calendar-alarm")


def _sleep(start="00:30", end="08:30"):
    return _event("event-sleep", start, end, CALENDAR_SLEEP_REF, event_type=EventType.SLEEP)


def test_events_overlapping_sleep_are_removed_whole():
    """수면보다 긴 밤사이 체류도 자르지 않고 통째로 지운다."""

    stay = _event("event-stay", "00:00", "09:30", title="밤사이 체류")
    morning = _event("event-move", "09:30", "10:10", event_type=EventType.MOVEMENT)
    draft = _draft(stay, _sleep(), morning)

    remove_events_overlapping_sleep(draft, make_request())

    assert [e.event_type for e in draft.events] == [EventType.SLEEP, EventType.MOVEMENT]
    assert draft.events[1].start_time == morning.start_time  # 겹치지 않은 event 는 그대로
    (warning,) = draft.warnings
    assert warning.severity is TimelineWarningSeverity.LOW
    assert "밤사이 체류" in warning.message


def test_partial_overlap_is_removed_too():
    """경계에 걸치기만 해도 지운다. 애매하게 남기지 않는다."""

    draft = _draft(_sleep(), _event("event-late", "08:00", "11:00"))

    remove_events_overlapping_sleep(draft, make_request())

    assert [e.event_type for e in draft.events] == [EventType.SLEEP]


def test_events_touching_sleep_boundaries_stay():
    """끝나는 순간에 시작하거나 시작하는 순간에 끝나는 event 는 겹치지 않는다."""

    before = _event("event-before", "00:00", "00:30")
    after = _event("event-after", "08:30", "09:30")
    draft = _draft(before, _sleep(), after)

    remove_events_overlapping_sleep(draft, make_request())

    assert len(draft.events) == 3
    assert draft.warnings == []


def test_wake_up_and_other_sleeps_are_kept():
    wake = _event("event-wake", "08:30", "08:30", event_type=EventType.WAKE_UP)
    nap_record = _event("event-sleep-2", "01:00", "07:00", SLEEP_REF, event_type=EventType.SLEEP)
    draft = _draft(_sleep(), nap_record, wake)

    remove_events_overlapping_sleep(draft, make_request())

    assert len(draft.events) == 3


def test_photo_and_calendar_of_removed_event_move_to_the_sleep():
    """사진은 사라지면 안 되고, 캘린더는 참조가 없으면 다음 확정에서 되살아나 또 지워진다."""

    stay = _event("event-stay", "00:00", "09:30", STAY_REF, PHOTO_REF, OTHER_CALENDAR_REF)
    draft = _draft(stay, _sleep())

    remove_events_overlapping_sleep(draft, make_request())

    (sleep,) = draft.events
    kept = {(ref.source_type, ref.raw_id) for ref in sleep.source_refs}
    assert (EventSourceType.PHOTO, fixture_raw_id("photo-1")) in kept
    assert (EventSourceType.CALENDAR, fixture_raw_id("calendar-alarm")) in kept
    assert (EventSourceType.STAY, fixture_raw_id("stay-1")) not in kept


def test_nothing_happens_without_a_sleep_event():
    draft = _draft(_event("event-stay", "00:00", "09:30"))

    remove_events_overlapping_sleep(draft, make_request())

    assert len(draft.events) == 1
    assert draft.warnings == []


def _calendar_sleep_day():
    request = make_request(
        stays=[
            stay_item(1, start=f"{DAY}T00:00:00", end=f"{DAY}T09:30:00", raw_id="stay-1", place="한빛아파트"),
        ],
        calendars=[
            calendar_item(1, "수면", start=f"{DAY}T00:30:00", end=f"{DAY}T08:30:00", raw_id="calendar-sleep"),
        ],
    )
    draft = _draft(_event("event-stay", "00:00", "09:30", title="밤사이 체류"), _sleep())
    return request, draft


def test_confirm_pass_removes_the_overlap_in_v3_sets_only():
    """v3 가 정한 규칙이라 v1·v2(운영) 세트의 확정에서는 돌지 않는다."""

    request, draft = _calendar_sleep_day()
    repair_draft(draft, request, extended=True)
    assert [e.event_type for e in draft.events] == [EventType.SLEEP]

    request, legacy = _calendar_sleep_day()
    repair_draft(legacy, request, extended=False)
    assert EventType.REST in [e.event_type for e in legacy.events]
