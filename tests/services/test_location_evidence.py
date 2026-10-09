"""체류를 일정·사진 event 의 위치 근거로 쓰기 (#138).

같은 방문을 그린 체류 카드와 일정·사진 카드가 따로 남는 것이 문제다. 코드는 규칙으로
정해지는 일만 한다 — 겹치는 체류를 위치 근거로 붙이고, 흡수 후보를 짚는다. 같은 방문인지는
Repair 가 판단해 `absorb_location_event` 로 흡수한다.

지켜야 하는 것: 일정·사진 event 는 어떤 단계도 지우거나 합치지 않는다. 같은 방문이 아닌
체류(근무 체류 안의 회의·점심)는 남아 하루에 빈칸이 생기지 않는다.
"""

from datetime import datetime

import pytest

from app.agents.repair.tools import RepairContext, available_tools
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.services.confirm_report import ConfirmReport
from app.services.draft_edit import DraftEditError, absorb_location_event
from app.services.draft_repair import repair_draft
from app.services.location_link import LINK_REASON, link_location_evidence
from app.services.location_only_overlap import find_location_only_overlaps
from app.services.photo_guard import enforce_photo_assignment
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    notification_item,
    photo_item,
    stay_item,
)

DAY = "2026-06-20"


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(_t(clock))


def _local(clock: str) -> str:
    return f"{DAY}T{clock}:00"


def _event(
    client_event_id: str,
    start: str,
    end: str,
    *refs: tuple[EventSourceType, str],
    event_type: EventType = EventType.UNKNOWN,
    title: str | None = None,
    place: str | None = None,
) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title or client_event_id,
        description="",
        start_time=_t(start),
        end_time=_t(end),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        place=place,
        source_refs=[
            SourceRef(source_type=source_type, raw_id=fixture_raw_id(label))
            for source_type, label in refs
        ],
    )


def _draft(*events: TimelineEventDraft) -> TimelineDraft:
    return TimelineDraft(user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events))


def _raw_ids(event: TimelineEventDraft) -> list[str]:
    return [ref.raw_id for ref in event.source_refs]


STAY = EventSourceType.STAY
CALENDAR = EventSourceType.CALENDAR
PHOTO = EventSourceType.PHOTO
NOTIFICATION = EventSourceType.NOTIFICATION


def _dentist_request(location_text: str | None = None):
    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-dentist",
                start=_local("13:55"),
                end=_local("15:05"),
                place="OO치과",
                places=["OO치과"],
                address="서울특별시 예시구 예시로 12",
            )
        ],
        calendars=[
            calendar_item(
                1,
                "치과 검진",
                start=_local("14:00"),
                end=_local("15:00"),
                raw_id="cal-dentist",
                location_text=location_text,
            )
        ],
    )


def _work_request():
    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-office",
                start=_local("09:00"),
                end=_local("18:00"),
                place="한빛타워",
                places=["한빛타워"],
            )
        ],
        calendars=[
            calendar_item(
                1,
                "주간 회의",
                start=_local("10:00"),
                end=_local("11:00"),
                raw_id="cal-meeting",
            )
        ],
        photos=[photo_item(1, taken=_local("12:20"), raw_id="photo-lunch")],
    )


def _work_draft() -> TimelineDraft:
    return _draft(
        _event("event-001", "09:00", "18:00", (STAY, "stay-office"), event_type=EventType.WORK),
        _event(
            "event-002", "10:00", "11:00", (CALENDAR, "cal-meeting"), event_type=EventType.MEETING
        ),
        _event("event-003", "12:00", "12:40", (PHOTO, "photo-lunch"), event_type=EventType.MEAL),
    )


# --- 위치 근거 연결 ---------------------------------------------------------------


def test_a_calendar_event_gets_the_overlapping_stay_as_location_evidence() -> None:
    request = _dentist_request()
    draft = _draft(_event("event-001", "14:00", "15:00", (CALENDAR, "cal-dentist")))

    link_location_evidence(draft, request)

    event = draft.events[0]
    stay_ref = event.source_refs[-1]
    assert stay_ref.raw_id == fixture_raw_id("stay-dentist")
    assert stay_ref.source_type is STAY
    assert stay_ref.reason == LINK_REASON
    # 시간과 문장은 그대로다.
    assert (event.start_time, event.end_time) == (_at("14:00"), _at("15:00"))


def test_a_calendar_that_names_another_place_is_not_linked() -> None:
    """일정은 계획이다. 다른 곳을 말하면 그 시간의 체류를 장소로 붙이지 않는다."""

    request = _dentist_request(location_text="스타벅스 강남점")
    draft = _draft(_event("event-001", "14:00", "15:00", (CALENDAR, "cal-dentist")))

    link_location_evidence(draft, request)

    assert _raw_ids(draft.events[0]) == [fixture_raw_id("cal-dentist")]


def test_a_similar_place_name_is_still_linked() -> None:
    request = _dentist_request(location_text="OO치과 본점")
    draft = _draft(_event("event-001", "14:00", "15:00", (CALENDAR, "cal-dentist")))

    link_location_evidence(draft, request)

    assert fixture_raw_id("stay-dentist") in _raw_ids(draft.events[0])


def test_a_photo_is_linked_only_to_the_stay_that_holds_its_taken_time() -> None:
    request = make_request(
        stays=[
            stay_item(1, raw_id="stay-cafe", start=_local("13:00"), end=_local("14:00")),
            stay_item(2, raw_id="stay-later", start=_local("15:00"), end=_local("16:00")),
        ],
        photos=[photo_item(1, taken=_local("13:30"), raw_id="photo-cake")],
    )
    draft = _draft(
        _event("event-001", "13:30", "13:30", (PHOTO, "photo-cake"), event_type=EventType.PHOTO_MOMENT)
    )

    link_location_evidence(draft, request)

    assert _raw_ids(draft.events[0]) == [
        fixture_raw_id("photo-cake"),
        fixture_raw_id("stay-cafe"),
    ]


def _placeless_meeting_request():
    """장소 없는 11시간 회의 일정과 그 시간의 여러 장소 체류 (#145, trace 468)."""

    def stay(raw_id: str, start: str, end: str, place: str, address: str):
        return stay_item(
            1, raw_id=raw_id, start=_local(start), end=_local(end), places=[place], address=address
        )

    return make_request(
        stays=[
            stay("stay-post-1", "12:08", "14:38", "포스트타워마포", "서울특별시 마포구 마포대로 89"),
            stay("stay-post-2", "14:44", "15:28", "포스트타워마포", "서울특별시 마포구 마포대로 89"),
            stay("stay-gongdeok", "17:57", "18:37", "공덕더샵아파트", "서울특별시 마포구 백범로 170"),
            stay("stay-post-3", "20:13", "20:36", "포스트타워마포", "서울특별시 마포구 마포대로 89"),
            stay("stay-station", "22:34", "22:59", "서울역", "서울특별시 용산구 청파로 378"),
        ],
        calendars=[
            calendar_item(
                1, "정기 회의", start=_local("12:00"), end=_local("23:00"), raw_id="cal-meeting"
            )
        ],
    )


def test_a_placeless_calendar_gets_only_the_stays_of_its_main_place() -> None:
    request = _placeless_meeting_request()
    draft = _draft(_event("event-001", "12:00", "23:00", (CALENDAR, "cal-meeting")))

    link_location_evidence(draft, request)

    assert _raw_ids(draft.events[0]) == [
        fixture_raw_id("cal-meeting"),
        fixture_raw_id("stay-post-1"),
        fixture_raw_id("stay-post-2"),
        fixture_raw_id("stay-post-3"),
    ]


def test_v3_confirm_reports_the_main_place_stay_card_and_keeps_the_others() -> None:
    request = _placeless_meeting_request()
    draft = _draft(
        _event(
            "event-001",
            "12:00",
            "23:00",
            (CALENDAR, "cal-meeting"),
            event_type=EventType.CALENDAR_EVENT,
            title="정기 회의",
        ),
        _event("event-002", "12:08", "15:28", (STAY, "stay-post-1"), (STAY, "stay-post-2")),
        _event("event-003", "17:57", "18:37", (STAY, "stay-gongdeok")),
    )
    report = ConfirmReport()

    confirmed = repair_draft(draft, request, report=report, extended=True)

    meeting = next(event for event in confirmed.events if event.title == "정기 회의")
    post_card = next(
        event for event in confirmed.events if fixture_raw_id("stay-post-1") in _raw_ids(event)
        and event is not meeting
    )
    assert meeting.place == "포스트타워마포"
    assert fixture_raw_id("stay-gongdeok") not in _raw_ids(meeting)
    # 공덕 체류는 회의 장소와 어긋나 흡수 후보가 아니다. 자기 카드로 남는다.
    (finding,) = [item for item in report.findings if item["kind"] == "LOCATION_ONLY_OVERLAP"]
    assert finding["clientEventId"] == post_card.client_event_id
    assert len(confirmed.events) == 3


def test_linking_twice_adds_nothing_more() -> None:
    request = _dentist_request()
    draft = _draft(_event("event-001", "14:00", "15:00", (CALENDAR, "cal-dentist")))

    link_location_evidence(draft, request)
    link_location_evidence(draft, request)

    assert len(draft.events[0].source_refs) == 2


def test_an_event_without_a_calendar_or_photo_is_not_linked() -> None:
    """대화는 어디서든 온다. 그 시간의 체류가 대화한 장소라는 뜻이 아니다."""

    request = make_request(
        stays=[stay_item(1, raw_id="stay-home", start=_local("20:00"), end=_local("23:00"))],
        notifications=[notification_item(1, "카카오톡", "민수", posted=_local("21:00"), raw_id="noti-1")],
    )
    draft = _draft(
        _event("event-001", "21:00", "21:30", (NOTIFICATION, "noti-1"), event_type=EventType.SOCIAL)
    )

    link_location_evidence(draft, request)

    assert _raw_ids(draft.events[0]) == [fixture_raw_id("noti-1")]


# --- 흡수 후보 찾기 ---------------------------------------------------------------


def test_a_stay_card_for_the_same_visit_is_reported_with_its_cover_ratio() -> None:
    request = _dentist_request()
    draft = _draft(
        _event("event-001", "13:55", "15:05", (STAY, "stay-dentist"), title="OO치과 체류"),
        _event("event-002", "14:00", "15:00", (CALENDAR, "cal-dentist"), title="치과 검진"),
    )

    (found,) = find_location_only_overlaps(draft, request)

    assert found.event.client_event_id == "event-001"
    (target,) = found.targets
    assert target.event.client_event_id == "event-002"
    assert target.cover_ratio == pytest.approx(60 / 70)


def test_a_long_work_stay_reports_every_event_inside_it() -> None:
    (found,) = find_location_only_overlaps(_work_draft(), _work_request())

    assert [target.event.client_event_id for target in found.targets] == [
        "event-002",
        "event-003",
    ]
    assert all(target.cover_ratio < 0.2 for target in found.targets)


def test_a_stay_at_a_different_place_from_the_calendar_is_not_reported() -> None:
    request = _dentist_request(location_text="스타벅스 강남점")
    draft = _draft(
        _event("event-001", "13:55", "15:05", (STAY, "stay-dentist")),
        _event("event-002", "14:00", "15:00", (CALENDAR, "cal-dentist")),
    )

    assert find_location_only_overlaps(draft, request) == []


def test_a_notification_only_event_is_not_a_target() -> None:
    request = make_request(
        stays=[stay_item(1, raw_id="stay-home", start=_local("20:00"), end=_local("23:00"))],
        notifications=[notification_item(1, "카카오톡", "민수", posted=_local("21:00"), raw_id="noti-1")],
    )
    draft = _draft(
        _event("event-001", "20:00", "23:00", (STAY, "stay-home")),
        _event("event-002", "21:00", "21:30", (NOTIFICATION, "noti-1"), event_type=EventType.SOCIAL),
    )

    assert find_location_only_overlaps(draft, request) == []


def _meeting_with_noted_stay_request(app_name: str):
    """회의 일정과 같은 시간의 체류, 그 사이에 온 알림 하나 (#145, trace 468)."""

    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-post",
                start=_local("12:08"),
                end=_local("15:28"),
                place="포스트타워마포",
                places=["포스트타워마포"],
            )
        ],
        calendars=[
            calendar_item(
                1, "정기 회의", start=_local("12:00"), end=_local("16:00"), raw_id="cal-meeting"
            )
        ],
        notifications=[
            notification_item(1, app_name, "알림", posted=_local("13:09"), raw_id="noti-1")
        ],
    )


def _meeting_with_noted_stay_draft() -> TimelineDraft:
    return _draft(
        _event(
            "event-001",
            "12:00",
            "16:00",
            (CALENDAR, "cal-meeting"),
            event_type=EventType.CALENDAR_EVENT,
        ),
        _event(
            "event-002",
            "12:08",
            "15:28",
            (STAY, "stay-post"),
            (NOTIFICATION, "noti-1"),
            event_type=EventType.REST,
        ),
    )


@pytest.mark.parametrize("app_name", ["YouTube", "카카오톡"])
def test_a_stay_card_with_an_unrelated_notification_is_still_reported(app_name: str) -> None:
    # 사전에 없는 앱이나 메신저의 알림은 어디서든 온다. 붙어 있어도 체류 카드다.
    request = _meeting_with_noted_stay_request(app_name)
    draft = _meeting_with_noted_stay_draft()

    [overlap] = find_location_only_overlaps(draft, request)

    assert overlap.event.client_event_id == "event-002"
    assert [target.event.client_event_id for target in overlap.targets] == ["event-001"]


@pytest.mark.parametrize("app_name", ["토스", "캐치테이블", "메시지"])
def test_a_stay_card_with_a_payment_or_reservation_notification_is_not_reported(
    app_name: str,
) -> None:
    # 결제·예약 알림은 그 자리에서 한 일을 말한다. 그런 event 는 체류 카드가 아니다.
    request = _meeting_with_noted_stay_request(app_name)
    draft = _meeting_with_noted_stay_draft()

    assert find_location_only_overlaps(draft, request) == []
    with pytest.raises(DraftEditError, match="체류뿐인 event 가 아닙니다"):
        absorb_location_event(draft, request, "event-002", "event-001")


def test_absorbing_a_stay_card_carries_its_unrelated_notification_along() -> None:
    request = _meeting_with_noted_stay_request("YouTube")
    draft = _meeting_with_noted_stay_draft()

    target = absorb_location_event(draft, request, "event-002", "event-001")

    assert [event.client_event_id for event in draft.events] == ["event-001"]
    assert _raw_ids(target) == [
        fixture_raw_id("cal-meeting"),
        fixture_raw_id("stay-post"),
        fixture_raw_id("noti-1"),
    ]


# --- 흡수 도구 --------------------------------------------------------------------


def test_absorbing_moves_the_stay_and_widens_the_event_so_no_time_is_lost() -> None:
    draft = _draft(
        _event("event-001", "13:55", "15:05", (STAY, "stay-dentist"), title="OO치과 체류"),
        _event(
            "event-002",
            "14:00",
            "15:00",
            (CALENDAR, "cal-dentist"),
            event_type=EventType.CALENDAR_EVENT,
            title="치과 검진",
        ),
    )

    target = absorb_location_event(draft, make_request(), "event-001", "event-002")

    assert [event.client_event_id for event in draft.events] == ["event-002"]
    assert target.title == "치과 검진"
    assert target.event_type is EventType.CALENDAR_EVENT
    assert (target.start_time, target.end_time) == (_at("13:55"), _at("15:05"))
    assert _raw_ids(target) == [fixture_raw_id("cal-dentist"), fixture_raw_id("stay-dentist")]


def test_absorbing_does_not_duplicate_an_already_cited_stay() -> None:
    draft = _draft(
        _event("event-001", "13:55", "15:05", (STAY, "stay-dentist")),
        _event("event-002", "14:00", "15:00", (CALENDAR, "cal-dentist"), (STAY, "stay-dentist")),
    )

    target = absorb_location_event(draft, make_request(), "event-001", "event-002")

    assert _raw_ids(target).count(fixture_raw_id("stay-dentist")) == 1


@pytest.mark.parametrize(
    ("source", "into", "message"),
    [
        ("event-002", "event-001", "체류뿐인 event 가 아닙니다"),  # 일정 event 는 흡수되지 않는다
        ("event-001", "event-003", "일정이나 사진 근거가 있는 event 가 아닙니다"),
        ("event-001", "event-004", "겹치지 않습니다"),
        ("event-001", "event-005", "60분을 넘습니다"),
    ],
)
def test_absorbing_refuses_what_would_lose_an_event_or_time(
    source: str, into: str, message: str
) -> None:
    draft = _draft(
        _event("event-001", "12:00", "15:00", (STAY, "stay-1")),
        _event("event-002", "13:00", "14:00", (CALENDAR, "cal-1")),
        _event("event-003", "13:00", "14:00", (STAY, "stay-2")),
        _event("event-004", "16:00", "17:00", (CALENDAR, "cal-2")),
        _event("event-005", "12:30", "13:10", (PHOTO, "photo-1"), event_type=EventType.MEAL),
    )

    with pytest.raises(DraftEditError, match=message):
        absorb_location_event(draft, make_request(), source, into)
    assert len(draft.events) == 5


def test_the_absorb_tool_is_only_offered_to_the_v3_set() -> None:
    draft = _draft()
    request = make_request()

    assert "absorb_location_event" in available_tools(
        RepairContext(request=request, draft=draft, extended=True)
    )
    assert "absorb_location_event" not in available_tools(
        RepairContext(request=request, draft=draft, extended=False)
    )


# --- 사진 단일 귀속과의 관계 ---------------------------------------------------------


def test_a_photo_event_that_loses_its_photo_does_not_survive_on_a_linked_stay() -> None:
    """붙인 체류는 사진이 있어서 붙은 것이다. 사진을 잃으면 체류 카드로 남지 않는다."""

    request = make_request(
        stays=[stay_item(1, raw_id="stay-cafe", start=_local("13:00"), end=_local("14:00"))],
        calendars=[
            calendar_item(1, "친구 만남", start=_local("13:00"), end=_local("14:00"), raw_id="cal-1")
        ],
        photos=[photo_item(1, taken=_local("13:30"), raw_id="photo-1")],
    )
    draft = _draft(
        _event("event-001", "13:00", "14:00", (CALENDAR, "cal-1"), (PHOTO, "photo-1")),
        _event("event-002", "13:30", "13:30", (PHOTO, "photo-1"), event_type=EventType.PHOTO_MOMENT),
    )
    link_location_evidence(draft, request)

    enforce_photo_assignment(draft, request)

    assert [event.client_event_id for event in draft.events] == ["event-001"]


# --- 확정 단계 --------------------------------------------------------------------


def test_v3_confirm_fills_the_calendar_place_from_the_linked_stay() -> None:
    request = _dentist_request()
    draft = _draft(_event("event-001", "14:00", "15:00", (CALENDAR, "cal-dentist"), title="치과 검진"))

    repair_draft(draft, request, extended=True)

    assert draft.events[0].place == "OO치과"
    assert fixture_raw_id("stay-dentist") in _raw_ids(draft.events[0])


def test_v2_confirm_does_not_link() -> None:
    request = _dentist_request()
    draft = _draft(_event("event-001", "14:00", "15:00", (CALENDAR, "cal-dentist"), title="치과 검진"))

    repair_draft(draft, request, extended=False)

    assert _raw_ids(draft.events[0]) == [fixture_raw_id("cal-dentist")]


def test_v3_confirm_keeps_work_meeting_and_lunch_and_reports_the_overlap() -> None:
    """근무 체류 안의 회의·점심은 셋 다 남는다. 코드는 짚기만 하고 지우지 않는다."""

    report = ConfirmReport()
    draft = repair_draft(_work_draft(), _work_request(), report=report, extended=True)

    assert sorted(event.event_type for event in draft.events) == sorted(
        [EventType.WORK, EventType.MEETING, EventType.MEAL]
    )
    meeting = next(event for event in draft.events if event.event_type is EventType.MEETING)
    lunch = next(event for event in draft.events if event.event_type is EventType.MEAL)
    assert fixture_raw_id("stay-office") in _raw_ids(meeting)
    assert fixture_raw_id("stay-office") in _raw_ids(lunch)

    (finding,) = [item for item in report.findings if item["kind"] == "LOCATION_ONLY_OVERLAP"]
    work = next(event for event in draft.events if event.event_type is EventType.WORK)
    assert finding["clientEventId"] == work.client_event_id
    assert {item["clientEventId"] for item in finding["overlappingEvents"]} == {
        meeting.client_event_id,
        lunch.client_event_id,
    }


def test_v3_confirm_twice_gives_the_same_result() -> None:
    request = _work_request()
    draft = repair_draft(_work_draft(), request, extended=True)
    first = draft.model_dump()

    repair_draft(draft, request, extended=True)

    assert draft.model_dump() == first
