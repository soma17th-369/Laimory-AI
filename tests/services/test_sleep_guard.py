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
    TimelineQuestion,
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


def _draft(*events, questions=()) -> TimelineDraft:
    return TimelineDraft(
        user_id="u",
        date=DAY,
        timezone="Asia/Seoul",
        events=list(events),
        questions=list(questions),
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


def test_removing_an_event_renumbers_ids_and_drops_dangling_question_refs():
    draft = _draft(
        _event("event-001", "03:00", "04:00", title="새벽의 유령"),
        _event("event-002", "09:00", "10:00", title="오전 작업"),
        questions=[
            TimelineQuestion(
                question_id="question-001",
                time_range={"startTime": _t("03:00"), "endTime": _t("04:00")},
                question="새벽에 무엇을 했나요?",
                reason="확인 필요",
                related_event_ids=["event-001", "event-002"],
            )
        ],
    )

    enforce_sleep_boundary(draft, _request())

    assert [event.client_event_id for event in draft.events] == ["event-001"]
    # 사라진 event 참조는 버리고, 남은 event 는 새 id 로 가리킨다.
    assert draft.questions[0].related_event_ids == ["event-001"]
