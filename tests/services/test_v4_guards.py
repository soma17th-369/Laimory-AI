"""v4 가 v3 의 강한 규칙을 잃지 않게 하는 확정 검사 (#167, 사용자 결정 2026-10-30).

같은 장면 사진은 event 하나, 캘린더 일정은 빠지지 않고 시간도 바뀌지 않음, 대화 event 하루 3개,
일정 근거 없는 일정 event 금지, 위치 근거 없는 체류 문장 금지, 제목에 장소 금지. 모두 실제
재실행에서 어긴 출력을 그대로 옮긴 것이다.
"""


from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import (
    CalendarItem,
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    MovementItem,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimeWindow,
)
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import build_timeline_clues
from app.services.confirm_report import ConfirmReport as _ConfirmReport
from app.services.conversation_guard import conversation_events, enforce_conversation_limit
from app.services.draft_repair import repair_draft, resolve_overlaps
from app.services.location_skeleton import build_location_skeleton
from app.services.photo_guard import merge_photo_scenes
from app.services.place_slots import fix_title_places
from app.services.skeleton_guard import (
    NEEDS_WRITING,
    remove_calendar_events_without_calendar,
    remove_stays_without_location,
    separate_movement_segments,
)
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    notification_item,
    photo_item,
    stay_item,
)

_KST = timezone(timedelta(hours=9))


def _at(hour, minute=0, day=20):
    return datetime(2026, 6, day, hour, minute, tzinfo=_KST)


def _event(refs, *, start, end, event_type=EventType.UNKNOWN, title="제목", description="", base=()):
    return TimelineEventDraft(
        client_event_id=f"event-{title}",
        event_type=event_type,
        title=title,
        description=description,
        start_time=start,
        end_time=end,
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=t, raw_id=r) for t, r in refs],
        base_event_ids=list(base),
    )


def _draft(*events):
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


def _clues(request, notifications=()):
    return build_timeline_clues(build_location_skeleton(request), [], [], list(notifications))


# --- 같은 장면 사진 -------------------------------------------------------------------------


def test_photos_of_one_scene_end_up_in_one_event():
    """2초 간격 저녁 사진 두 장이 식사 event 둘이 된 실제 출력."""

    a = photo_item("a", taken="2026-06-20T22:56:45")
    b = photo_item("b", taken="2026-06-20T22:56:47")
    request = make_request(photos=[a, b])
    soup = _event([(EventSourceType.PHOTO, a.raw_id)], start=_at(22, 56), end=_at(23, 16), event_type=EventType.MEAL, title="국물 요리")
    pork = _event([(EventSourceType.PHOTO, b.raw_id)], start=_at(22, 56), end=_at(23, 16), event_type=EventType.MEAL, title="수육과 김치")
    draft = _draft(soup, pork)

    merge_photo_scenes(draft, request)

    assert len(draft.events) == 1
    assert {ref.raw_id for ref in draft.events[0].source_refs} == {a.raw_id, b.raw_id}


def test_photos_far_apart_in_time_stay_separate():
    a = photo_item("a", taken="2026-06-20T12:00:00")
    b = photo_item("b", taken="2026-06-20T13:00:00")
    request = make_request(photos=[a, b])
    lunch = _event([(EventSourceType.PHOTO, a.raw_id)], start=_at(12), end=_at(12, 20), title="점심")
    later = _event([(EventSourceType.PHOTO, b.raw_id)], start=_at(13), end=_at(13), title="다른 장면")
    draft = _draft(lunch, later)

    merge_photo_scenes(draft, request)

    assert len(draft.events) == 2


def test_unbundled_photos_of_one_scene_share_one_bundle():
    a = photo_item("a", taken="2026-06-20T22:56:45")
    b = photo_item("b", taken="2026-06-20T22:56:47")
    request = make_request(photos=[a, b])
    clues = build_timeline_clues(
        build_location_skeleton(request), [], [
            PhotoClue(clue_id="P1", raw_id=a.raw_id, taken_at=_at(22, 56)),
            PhotoClue(clue_id="P2", raw_id=b.raw_id, taken_at=_at(22, 56)),
        ], []
    )

    photo_bundles = [bundle for bundle in clues.bundles if bundle.photos]

    assert len(photo_bundles) == 1
    assert [photo.clue_id for photo in photo_bundles[0].photos] == ["P1", "P2"]


# --- 대화 event 하루 3개 ---------------------------------------------------------------------


def test_v4_counts_conversations_by_clue_including_text_messages():
    """카카오톡 셋과 대화로 쓴 문자(`메시지`) 하나 — 앱 사전으로 세면 문자가 빠져 4개가 남았다."""

    chats = [notification_item(f"c{i}", "카카오톡", f"친구{i}", posted=f"2026-06-20T1{i}:00:00") for i in range(3)]
    sms = notification_item("sms", "메시지", "[전화번호]", posted="2026-06-20T17:02:00")
    request = make_request(notifications=[*chats, sms])
    events = [
        _event([(EventSourceType.NOTIFICATION, n.raw_id)], start=_at(10 + i), end=_at(10 + i), event_type=EventType.SOCIAL, title=f"대화{i}")
        for i, n in enumerate([*chats, sms])
    ]
    draft = _draft(*events)

    enforce_conversation_limit(draft, request, conversation_ids={n.raw_id for n in [*chats, sms]})

    assert len(draft.events) == 3


def test_v4_confirm_pass_itself_keeps_three_conversations():
    """확정 단계에 실제로 걸렸는지 본다 — v4 단계 목록이 대화 검사를 앱 사전 쪽으로 걸어 둔 채였다."""

    notes = [
        notification_item("ig", "Instagram", "MJ관계연구소", posted="2026-06-20T16:00:00"),
        notification_item("kt1", "카카오톡", "울동생", posted="2026-06-20T16:45:00"),
        notification_item("kt2", "카카오톡", "욱희민영", posted="2026-06-20T19:15:00"),
        notification_item("deal", "당근", "거래 완료", posted="2026-06-20T19:20:00"),
        notification_item("kt3", "카카오톡", "하니", posted="2026-06-20T21:23:00"),
    ]
    request = make_request(notifications=notes)
    clues = [
        NotificationClue(clue_id=f"N{i}", kind=NotificationClueKind.CONVERSATION, counterpart="상대", summary="대화", raw_ids=[n.raw_id])
        for i, n in enumerate([notes[0], notes[1], notes[2], notes[4]], start=1)
    ] + [NotificationClue(clue_id="N9", kind=NotificationClueKind.NEWS, summary="거래", raw_ids=[notes[3].raw_id])]
    bundles = build_timeline_clues(build_location_skeleton(request), [], [], clues)
    events = [
        _event([(EventSourceType.NOTIFICATION, notes[0].raw_id)], start=_at(16), end=_at(16), event_type=EventType.SOCIAL, title="A"),
        _event([(EventSourceType.NOTIFICATION, notes[1].raw_id)], start=_at(16, 45), end=_at(17, 21), event_type=EventType.SOCIAL, title="B"),
        _event([(EventSourceType.NOTIFICATION, notes[2].raw_id), (EventSourceType.NOTIFICATION, notes[3].raw_id)], start=_at(19, 15), end=_at(19, 38), event_type=EventType.SOCIAL, title="C"),
        _event([(EventSourceType.NOTIFICATION, notes[4].raw_id)], start=_at(21, 23), end=_at(21, 29), event_type=EventType.SOCIAL, title="D"),
    ]
    draft = _draft(*events)

    repair_draft(draft, request, extended=True, clues=bundles)

    assert len([e for e in draft.events if e.event_type is EventType.SOCIAL]) == 3


# --- 캘린더 ---------------------------------------------------------------------------------


def test_notification_cannot_become_a_calendar_event():
    """Gmail·문자로 온 예비소집 안내를 일정 event 로 만든 실제 출력."""

    mail = notification_item("mail", "Gmail", "예비소집 안내", posted="2026-06-20T16:56:00")
    stay = stay_item("stay", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00")
    request = make_request(stays=[stay], notifications=[mail])
    only_mail = _event([(EventSourceType.NOTIFICATION, mail.raw_id)], start=_at(11), end=_at(23, 59), event_type=EventType.CALENDAR_EVENT, title="예비소집 안내")
    with_stay = _event([(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, mail.raw_id)], start=_at(9), end=_at(18), event_type=EventType.CALENDAR_EVENT, title="체류")
    draft = _draft(only_mail, with_stay)

    remove_calendar_events_without_calendar(draft, request, _clues(request))

    assert draft.events == [with_stay]
    assert with_stay.event_type is EventType.STAY
    assert with_stay.merge_reason == NEEDS_WRITING, "예매 알림으로 쓴 `일정이 있었다` 가 그대로 남은 실제 출력."


def test_a_calendar_event_with_only_a_stay_becomes_a_stay():
    """일정 근거가 없고 체류만 남으면 그 체류의 event 다. `UNKNOWN` 은 체류를 뜻하지 않는다(#172)."""

    stay = stay_item("stay", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00")
    request = make_request(stays=[stay])
    event = _event([(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(18), event_type=EventType.CALENDAR_EVENT, title="체류")
    draft = _draft(event)

    remove_calendar_events_without_calendar(draft, request, _clues(request))

    assert draft.events == [event]
    assert event.event_type is EventType.STAY


def test_a_stay_type_without_location_is_not_a_stay():
    """위치 근거 없이 체류를 말하는 event 는 타입이 `STAY` 여도 지운다."""

    chat = notification_item("mom", "카카오톡", "엄마", posted="2026-06-20T22:01:00")
    request = make_request(notifications=[chat])
    night = _event(
        [(EventSourceType.NOTIFICATION, chat.raw_id)], start=_at(22), end=_at(23), event_type=EventType.STAY,
        title="도화동에서 보낸 밤", description="밤을 보냈다.",
    )
    draft = _draft(night)

    remove_stays_without_location(draft, request, _clues(request))

    assert draft.events == []


def test_calendar_event_spanning_a_movement_keeps_its_time():
    """일정 시간은 바꾸지 않는다 — 이동을 품은 일정 event 를 이동 앞뒤로 자르지 않는다."""

    home = stay_item("home", lat=37.27, start="2026-06-20T06:00:00", end="2026-06-20T08:30:00")
    office = stay_item("office", lat=37.54, start="2026-06-20T09:10:00", end="2026-06-20T18:00:00")
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at="2026-06-20T08:30:00", end_at="2026-06-20T09:10:00",
        start=GeoPlace(latitude=37.27, longitude=127.0), end=GeoPlace(latitude=37.54, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=30000,
    )
    workshop = calendar_item("w", "워크숍", start="2026-06-20T08:00:00", end="2026-06-20T12:00:00")
    request = make_request(stays=[home, office], movements=[commute], calendars=[workshop])
    event = _event(
        [(EventSourceType.CALENDAR, workshop.raw_id), (EventSourceType.STAY, office.raw_id)],
        start=_at(8), end=_at(12), event_type=EventType.CALENDAR_EVENT, title="워크숍",
    )
    draft = _draft(event)

    separate_movement_segments(draft, request, _clues(request))

    assert (event.start_time, event.end_time) == (_at(8), _at(12))


def test_v4_confirm_never_loses_a_calendar_event():
    """Timeline 이 일정을 통째로 빠뜨려도 확정이 끝나면 일정 event 가 있고 장소 후보도 받는다."""

    office = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00", places=["OO타워"], address="서울 마포구 도화동 1")
    meeting = calendar_item("m", "주간 회의", start="2026-06-20T10:00:00", end="2026-06-20T11:00:00", location_text="OO타워")
    request = make_request(stays=[office], calendars=[meeting])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    draft = _draft(
        _event([(EventSourceType.STAY, office.raw_id)], start=_at(9), end=_at(18), event_type=EventType.WORK, title="업무", description="{place}에서 일했다.", base=["E1"])
    )

    repair_draft(draft, request, extended=True, clues=clues)

    calendar_events = [
        e for e in draft.events if any(r.raw_id == meeting.raw_id for r in e.source_refs)
    ]
    assert len(calendar_events) == 1
    assert calendar_events[0].place_candidates


# --- 위치 근거 없는 체류 문장 ------------------------------------------------------------------


def test_night_made_of_dinner_photos_after_location_ends_is_removed():
    """22시에 위치 기록이 끊긴 뒤 저녁 사진과 대화로 만든 `도화동에서 보낸 밤` (실제 출력)."""

    office = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T22:00:00")
    dinner = photo_item("dinner", taken="2026-06-20T22:56:00")
    chat = notification_item("mom", "카카오톡", "엄마", posted="2026-06-20T22:01:00")
    request = make_request(stays=[office], photos=[dinner], notifications=[chat])
    night = _event(
        [(EventSourceType.NOTIFICATION, chat.raw_id), (EventSourceType.PHOTO, dinner.raw_id)],
        start=_at(22), end=_at(23, 16), title="도화동에서 보낸 밤", description="밤을 보냈다.",
    )
    meal = _event([(EventSourceType.PHOTO, dinner.raw_id)], start=_at(22, 56), end=_at(23, 16), event_type=EventType.MEAL, title="한식 식사", description="국물 요리를 먹었다.")
    draft = _draft(night, meal)

    remove_stays_without_location(draft, request, _clues(request))

    assert draft.events == [meal], "사진으로 무엇을 했는지 말하는 event 는 남는다."


def test_photo_meal_that_spent_time_at_the_restaurant_is_not_a_stay_without_location():
    """식사 문장 뼈대 `먹으며 시간을 보냈다` 는 체류 문장이 아니다 — 사진만 근거인 식사가 지워졌다."""

    lunch = photo_item("lunch", taken="2026-06-20T12:58:00")
    request = make_request(photos=[lunch])
    meal = _event(
        [(EventSourceType.PHOTO, lunch.raw_id)], start=_at(12, 58), end=_at(13, 18),
        event_type=EventType.MEAL, title="한식 식사", description="{place}에서 한식을 먹으며 시간을 보냈다.",
    )
    draft = _draft(meal)

    remove_stays_without_location(draft, request, _clues(request))

    assert draft.events == [meal]


def test_address_written_without_its_number_becomes_the_slot():
    """Repair 가 쓴 `전남광주통합특별시 여수시 여서동에서 …` — 번지 없는 주소도 칸으로 되돌린다."""

    from app.services.place_slots import fill_place_slots

    stage = stay_item("stage", start="2026-06-20T12:00:00", end="2026-06-20T12:30:00", places=["K-가든 페스티벌"], address="전남광주통합특별시 여수시 여서동 222-1")
    request = make_request(stays=[stage])
    event = _event([(EventSourceType.STAY, stage.raw_id)], start=_at(12), end=_at(12, 30), title="행사 구경", description="전남광주통합특별시 여수시 여서동에서 행사를 봤다.")
    event.place_candidates = ["K-가든 페스티벌"]
    event.place = "K-가든 페스티벌"

    fill_place_slots(_draft(event), request)

    assert event.description == "{place}에서 행사를 봤다."


# --- 제목에 장소 금지 ------------------------------------------------------------------------


def test_title_never_carries_a_place_that_is_not_the_area():
    """사진 해석의 `한식당에서` 를 옮긴 `한식당에서 저녁 식사` (6회 중 4회)."""

    office = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T22:00:00", address="서울 마포구 도화동 17-14")
    dinner = photo_item("dinner", taken="2026-06-20T22:56:00")
    request = make_request(stays=[office], photos=[dinner])
    clues = _clues(request)
    invented = _event([(EventSourceType.PHOTO, dinner.raw_id)], start=_at(22, 56), end=_at(23, 16), event_type=EventType.MEAL, title="한식당에서 저녁 식사")
    work = _event([(EventSourceType.STAY, office.raw_id)], start=_at(9), end=_at(22), event_type=EventType.WORK, title="마포구 도화동에서 업무")
    work.address = "서울 마포구 도화동 17-14"
    restaurant = _event([(EventSourceType.STAY, office.raw_id)], start=_at(12), end=_at(13), event_type=EventType.MEAL, title="한식당에서 점심")
    restaurant.address = "서울 마포구 도화동 17-14"

    fix_title_places(_draft(invented, work, restaurant), request, clues)

    assert invented.title == "저녁 식사"
    assert work.title == "마포구 도화동에서 업무"
    assert restaurant.title == "마포구 도화동에서 점심"


def test_place_name_anywhere_in_the_title_is_removed():
    """위치·사진의 장소명을 제목 가운데에 넣은 경우(`해녀밥상 점심`)도 뺀다. 지명은 남긴다."""

    lunch = photo_item("lunch", taken="2026-06-20T12:58:00", lat=34.74, lon=127.73)
    lunch.places = ["해녀밥상", "그랜드원룸"]
    lunch.address = "전남광주통합특별시 여수시 통제영3길 12-15"
    request = make_request(photos=[lunch])
    meal = _event([(EventSourceType.PHOTO, lunch.raw_id)], start=_at(12, 58), end=_at(13, 18), event_type=EventType.MEAL, title="여수시 통제영3길에서 해녀밥상 점심")
    meal.address = lunch.address

    fix_title_places(_draft(meal), request, _clues(request))

    assert meal.title == "여수시 통제영3길에서 점심"


def test_venue_words_never_stay_in_the_title():
    """`한식당 식사` 처럼 입력에 없는 가게 종류도 제목에는 장소다. 음식 종류는 남긴다."""

    dinner = photo_item("dinner", taken="2026-06-20T22:56:00")
    request = make_request(photos=[dinner])
    korean = _event([(EventSourceType.PHOTO, dinner.raw_id)], start=_at(22, 56), end=_at(23, 16), event_type=EventType.MEAL, title="한식당 식사")
    cafe = _event([(EventSourceType.PHOTO, dinner.raw_id)], start=_at(15), end=_at(15, 30), event_type=EventType.MEAL, title="카페에서 커피")

    fix_title_places(_draft(korean, cafe), request, _clues(request))

    assert korean.title == "한식 식사"
    assert cafe.title == "커피"


def test_movement_title_never_carries_a_place_name():
    """`동백호수공원 두산위브더제니스에서 도화동으로 이동` — 이동 제목도 지명만 쓴다."""

    home = stay_item("home", start="2026-06-20T06:00:00", end="2026-06-20T08:00:00", places=["동백호수공원 두산위브더제니스"])
    request = make_request(stays=[home])
    move = _event([(EventSourceType.STAY, home.raw_id)], start=_at(8), end=_at(9), event_type=EventType.MOVEMENT, title="동백호수공원 두산위브더제니스에서 도화동으로 이동")

    fix_title_places(_draft(move), request, _clues(request))

    assert move.title == "도화동으로 이동"


# --- 뼈대 구간 시간과 다른 장소 -------------------------------------------------------------------


def _office_cafe_request(*, calendars=()):
    """사무실(09~13시)과 이동 기록 없이 2km 넘게 떨어진 카페(14~18시). 13~14시는 빈 시간이다."""

    office = stay_item("office", lat=37.5446, lon=126.9520, start="2026-06-20T09:00:00", end="2026-06-20T13:00:00",
                       places=["OO타워"], address="서울 마포구 도화동 2")
    cafe = stay_item("cafe", lat=37.5650, lon=126.9370, start="2026-06-20T14:00:00", end="2026-06-20T18:00:00",
                     places=["연희카페"], address="서울 서대문구 연희동 5")
    request = make_request(stays=[office, cafe], calendars=list(calendars))
    return request, office, cafe


def test_v4_confirm_keeps_the_movement_where_the_skeleton_put_it():
    """체류 09~12시와 이동 원본 11~13시. 뼈대는 이동을 12~13시로 정리하는데, 예전 근거 구간 정렬이
    원본 전체로 되늘려 체류와 한 시간 겹쳤다(외부 검토 지적). 빈 초안에서도 재현됐다."""

    home = stay_item("home", lat=37.5446, lon=126.9520, start="2026-06-20T09:00:00", end="2026-06-20T12:00:00")
    trip = MovementItem(
        raw_id=fixture_raw_id("movement-trip"), start_at="2026-06-20T11:00:00", end_at="2026-06-20T13:00:00",
        start=GeoPlace(latitude=37.5446, longitude=126.9520), end=GeoPlace(latitude=37.60, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=8000,
    )
    request = make_request(stays=[home], movements=[trip])
    clues = _clues(request)
    draft = _draft()

    repair_draft(draft, request, extended=True, clues=clues)

    movement = next(e for e in draft.events if e.event_type is EventType.MOVEMENT)
    assert (movement.start_time, movement.end_time) == (_at(12), _at(13))
    stay = next(e for e in draft.events if e.event_type is not EventType.MOVEMENT)
    assert stay.end_time <= movement.start_time


def test_v4_confirm_separates_two_places_merged_across_an_empty_time():
    """Timeline 이 사무실과 카페를 09~18시 한 event 로 합치면 다른 장소라 끊는다(사용자 결정).

    이동 기록이 없어 이동 구간 분리가 닿지 않던 자리다. 근거가 긴 쪽만 남기고 다른 쪽은 틀 문장으로
    되살린다. 13~14시 빈 시간은 어느 event 도 덮지 않는다.
    """

    request, office, cafe = _office_cafe_request()
    clues = _clues(request)
    merged = _event(
        [(EventSourceType.STAY, office.raw_id), (EventSourceType.STAY, cafe.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.WORK, title="업무", description="{place}에서 일했다.",
        base=["E1", "E2"],
    )
    draft = _draft(merged)

    repair_draft(draft, request, extended=True, clues=clues)

    holders = {
        raw_id: [e for e in draft.events if any(r.raw_id == raw_id for r in e.source_refs)]
        for raw_id in (office.raw_id, cafe.raw_id)
    }
    assert len(holders[office.raw_id]) == 1 and len(holders[cafe.raw_id]) == 1
    assert holders[office.raw_id][0] is not holders[cafe.raw_id][0], "다른 장소는 한 event 에 있지 않다."
    assert not [e for e in draft.events if e.start_time < _at(14) and _at(13) < e.end_time], "빈 시간은 비워 둔다."


def test_v4_calendar_spanning_two_places_keeps_its_time_and_the_other_place_comes_back():
    """일정은 그대로 두되, 일정 시간 안에 다른 곳에 있었으면 그 위치 구간도 따로 남긴다(사용자 결정).

    "일정을 그렇게 등록해놔도 잠깐 바깥에 나가서 카페에 있을 수도 있고 그렇기 때문에 위치 뼈대는
    살리는 게 맞다."
    """

    meeting = calendar_item("m", "업무", start="2026-06-20T10:00:00", end="2026-06-20T16:00:00")
    request, office, cafe = _office_cafe_request(calendars=[meeting])
    clues = _clues(request)
    work = _event(
        [(EventSourceType.CALENDAR, meeting.raw_id), (EventSourceType.STAY, office.raw_id), (EventSourceType.STAY, cafe.raw_id)],
        start=_at(10), end=_at(16), event_type=EventType.WORK, title="업무", description="{place}에서 일했다.",
        base=["E1", "E2"],
    )
    draft = _draft(work)

    repair_draft(draft, request, extended=True, clues=clues)

    calendar_event = next(e for e in draft.events if any(r.raw_id == meeting.raw_id for r in e.source_refs))
    assert (calendar_event.start_time, calendar_event.end_time) == (_at(10), _at(16))
    assert not any(r.raw_id == cafe.raw_id for r in calendar_event.source_refs)
    assert [e for e in draft.events if e is not calendar_event and any(r.raw_id == cafe.raw_id for r in e.source_refs)]


def test_v4_movement_that_starts_inside_a_sleep_plan_is_kept():
    """수면 일정(00:30~08:30)은 계획이고 08:09 에 실제로 이동했다. 실제 재실행에서 수면과 겹친다고
    출근 이동이 지워졌다. 이동과 다른 장소의 체류는 수면 계획보다 기록이 맞다(사용자 결정: 일정은 그대로,
    위치 뼈대도 살린다)."""

    home = stay_item("home", lat=35.8275, lon=128.6283, start="2026-06-20T00:00:00", end="2026-06-20T08:09:00",
                     address="대구 수성구 지범로17길 85")
    school = stay_item("school", lat=35.8880, lon=128.6110, start="2026-06-20T08:54:00", end="2026-06-20T10:22:00",
                       address="대구 북구 대현동 1")
    trip = MovementItem(
        raw_id=fixture_raw_id("movement-trip"), start_at="2026-06-20T08:09:00", end_at="2026-06-20T08:54:00",
        start=GeoPlace(latitude=35.8275, longitude=128.6283), end=GeoPlace(latitude=35.8880, longitude=128.6110),
        transports=["IN_VEHICLE"], distance_meters=8000,
    )
    sleep_plan = calendar_item("sleep", "수면 zzz", start="2026-06-20T00:30:00", end="2026-06-20T08:30:00")
    request = make_request(stays=[home, school], movements=[trip], calendars=[sleep_plan])
    clues = _clues(request)
    draft = _draft(
        _event([(EventSourceType.CALENDAR, sleep_plan.raw_id), (EventSourceType.STAY, home.raw_id)],
               start=_at(0, 30), end=_at(8, 30), event_type=EventType.SLEEP, title="잠", description="{place}에서 잠을 잤다.", base=["E1"]),
        _event([(EventSourceType.MOVEMENT, trip.raw_id)], start=_at(8, 9), end=_at(8, 54),
               event_type=EventType.MOVEMENT, title="이동", base=["E2"]),
        _event([(EventSourceType.STAY, school.raw_id)], start=_at(8, 54), end=_at(10, 22), title="학교", base=["E3"]),
    )

    repair_draft(draft, request, extended=True, clues=clues)

    movements = [e for e in draft.events if e.event_type is EventType.MOVEMENT]
    assert [(m.start_time, m.end_time) for m in movements] == [(_at(8, 9), _at(8, 54))]
    sleep = next(e for e in draft.events if e.event_type is EventType.SLEEP)
    assert (sleep.start_time, sleep.end_time) == (_at(0, 30), _at(8, 30)), "수면 일정 시간은 그대로다."


def test_v4_restore_does_not_fill_a_gap_without_records_inside_a_stay():
    """같은 집 구간 안에서 기록이 없는 00:02~00:46 을 틀 문장으로 채우자 근거 기록과 겹치지 않아
    지명도 장소 후보도 없는 `보낸 시간` 이 남았다(실제 재실행). 되살리는 것은 기록이 덮는 시간이다."""

    late = stay_item("late", lat=35.8275, lon=128.6283, start="2026-06-20T18:49:00", end="2026-06-21T00:02:00",
                     places=["화성파크드림"], address="대구 수성구 지범로17길 85")
    night = stay_item("night", lat=35.8275, lon=128.6283, start="2026-06-21T00:46:00", end="2026-06-21T05:00:00",
                      places=["화성파크드림"], address="대구 수성구 지범로17길 85")
    sleep_plan = calendar_item("sleep", "수면 zzz", start="2026-06-21T00:30:00", end="2026-06-21T05:00:00")
    request = make_request(stays=[late, night], calendars=[sleep_plan],
                           window=TimeWindow(start="2026-06-20T18:00:00", end="2026-06-21T05:00:00"))
    clues = _clues(request)
    day2 = lambda h, m=0: _at(h, m, day=21)
    draft = _draft(
        _event([(EventSourceType.STAY, late.raw_id)], start=_at(18, 49), end=day2(0, 2), title="집", base=["E1"]),
        _event([(EventSourceType.CALENDAR, sleep_plan.raw_id), (EventSourceType.STAY, late.raw_id), (EventSourceType.STAY, night.raw_id)],
               start=day2(0, 30), end=day2(5), event_type=EventType.SLEEP, title="잠", description="{place}에서 잠을 잤다.", base=["E1"]),
    )

    repair_draft(draft, request, extended=True, clues=clues)

    in_gap = [e for e in draft.events if e.start_time < day2(0, 30) and day2(0, 2) < e.end_time and e.event_type is not EventType.SLEEP]
    assert not in_gap, [(e.title, e.start_time, e.end_time) for e in in_gap]


def test_v4_stay_time_cut_by_a_later_step_comes_back():
    """Timeline 이 대현동 체류 전체(10:57~17:45, 407분)를 점심 `MEAL` 로 채웠고, 위치 구간 복원 뒤에 도는
    식사 시간 단계가 20분으로 줄여 오후 체류가 통째로 비었다(실제 재실행). 시간을 바꾸는 단계가 모두
    끝난 뒤 위치 구간을 다시 확인한다."""

    campus = stay_item("campus", lat=35.8880, lon=128.6110, start="2026-06-20T10:57:00", end="2026-06-20T17:45:00",
                       places=["경북대학교"], address="대구 북구 대현동 1")
    lunch = photo_item("lunch", taken="2026-06-20T12:49:00", lat=35.8880, lon=128.6110)
    request = make_request(stays=[campus], photos=[lunch])
    clues = build_timeline_clues(
        build_location_skeleton(request), [],
        [PhotoClue(id="P1", rawId=lunch.raw_id, takenAt=_at(12, 49), latitude=35.8880, longitude=128.6110, activity="점심을 먹었다")], []
    )
    draft = _draft(
        _event([(EventSourceType.STAY, campus.raw_id), (EventSourceType.PHOTO, lunch.raw_id)],
               start=_at(10, 57), end=_at(17, 45), event_type=EventType.MEAL, title="점심 식사",
               description="{place}에서 점심을 먹었다.", base=["E1"]),
    )

    repair_draft(draft, request, extended=True, clues=clues)

    covered = sorted((e.start_time, e.end_time) for e in draft.events)
    assert covered[0][0] == _at(10, 57) and covered[-1][1] == _at(17, 45), covered
    meal = next(e for e in draft.events if e.event_type is EventType.MEAL)
    assert meal.end_time - meal.start_time <= timedelta(hours=1)



# --- 꺼낸 event 가 사진을 갖는다 ------------------------------------------------------------


def test_v4_photo_stays_with_the_event_extracted_from_it():
    """Timeline 이 음식 사진으로 식사를 꺼내고 같은 사진을 회의 근거에도 남긴 실제 출력.

    다른 근거(일정·체류)가 있는 회의가 사진을 가져가 사진뿐이던 식사가 지워졌다. 꺼냈으면 원래 event
    에서 뺀다(사용자 결정 2026-10-30, "꺼냈으면 원래거는 지워야지") — 회의와 식사 둘 다 남는다.
    """

    from app.services.photo_guard import enforce_photo_scenes

    office = stay_item("office", start="2026-06-20T12:08:00", end="2026-06-20T20:36:00")
    meeting_cal = calendar_item("meeting", "팀 정기 회의", start="2026-06-20T12:00:00", end="2026-06-20T23:00:00")
    food = photo_item("food", taken="2026-06-20T17:24:00")
    request = make_request(stays=[office], calendars=[meeting_cal], photos=[food])
    meeting = _event(
        [(EventSourceType.CALENDAR, meeting_cal.raw_id), (EventSourceType.STAY, office.raw_id), (EventSourceType.PHOTO, food.raw_id)],
        start=_at(12), end=_at(23), event_type=EventType.MEETING, title="회의",
    )
    meal = _event(
        [(EventSourceType.PHOTO, food.raw_id)], start=_at(17, 24), end=_at(18, 4), event_type=EventType.MEAL, title="식사",
    )
    draft = _draft(meeting, meal)

    enforce_photo_scenes(draft, request)

    assert [e.title for e in draft.events] == ["회의", "식사"], "둘 다 남는다."
    assert food.raw_id in [r.raw_id for r in meal.source_refs]
    assert food.raw_id not in [r.raw_id for r in meeting.source_refs], "꺼냈으면 원래 event 에서 뺀다."


def test_v4_same_scene_photos_gather_in_the_extracted_event():
    """같은 장면(12:49)의 음식 사진은 식사에, 두 사람 사진은 체류에 있을 때 짧은 식사 쪽으로 모은다 —
    체류 쪽으로 모으면 음식 사진이 7시간 체류를 `점심을 먹으며` 로 만든다."""

    from app.services.photo_guard import merge_photo_scenes

    stay = stay_item("campus", start="2026-06-20T10:57:00", end="2026-06-20T17:45:00")
    food = photo_item("food", taken="2026-06-20T12:49:00", lat=35.89, lon=128.61)
    together = photo_item("together", taken="2026-06-20T12:49:30", lat=35.89, lon=128.61)
    request = make_request(stays=[stay], photos=[food, together])
    stayed = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.PHOTO, together.raw_id)],
        start=_at(10, 57), end=_at(17, 45), title="보낸 시간",
    )
    meal = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.PHOTO, food.raw_id)],
        start=_at(12, 49), end=_at(13, 9), event_type=EventType.MEAL, title="식사",
    )
    draft = _draft(stayed, meal)

    merge_photo_scenes(draft, request)

    assert {r.raw_id for r in meal.source_refs} >= {food.raw_id, together.raw_id}
    assert together.raw_id not in [r.raw_id for r in stayed.source_refs]
    assert [e.title for e in draft.events] == ["보낸 시간", "식사"]


def test_v4_does_not_send_location_only_overlap_to_repair():
    """v4 는 `LOCATION_ONLY_OVERLAP` 을 돌리지 않는다(사용자 결정 2026-10-31). 체류 안의 식사는 정상 구조인데,
    이 검사를 받은 Repair 가 7시간 체류를 식사로 고쳐 썼다."""

    from app.services.confirm_report import ConfirmReport

    stay = stay_item("campus", start="2026-06-20T10:57:00", end="2026-06-20T17:45:00")
    food = photo_item("food", taken="2026-06-20T12:49:00")
    request = make_request(stays=[stay], photos=[food])
    stayed = _event([(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title="보낸 시간", base=["E1"])
    meal = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.PHOTO, food.raw_id)],
        start=_at(12, 49), end=_at(13, 9), event_type=EventType.MEAL, title="점심", base=["E1"],
    )
    report = ConfirmReport()

    repair_draft(_draft(stayed, meal), request, report=report, clues=_clues(request))

    assert [item for item in report.findings if item["kind"] == "LOCATION_ONLY_OVERLAP"] == []


def test_v4_location_evidence_matches_the_event_time():
    """기본 event 에서 온 식사의 근거에 그 구간의 기록이 모두 실린 실제 출력 — 17:24 식사에 12:08 체류.

    event 와 근거의 연계는 시간이 맞아야 한다(사용자 결정 2026-10-31). 앞뒤 20분까지는 그 시간의 기록이다.
    근거가 모두 빠지는 event 는 건드리지 않는다.
    """

    from app.services.skeleton_guard import settle_event_evidence

    morning = stay_item("am", start="2026-06-20T12:08:00", end="2026-06-20T14:38:00")
    evening = stay_item("pm", start="2026-06-20T17:00:00", end="2026-06-20T20:36:00")
    request = make_request(stays=[morning, evening])
    clues = _clues(request)
    meal = _event(
        [(EventSourceType.STAY, morning.raw_id), (EventSourceType.STAY, evening.raw_id)],
        start=_at(17, 24), end=_at(18, 4), event_type=EventType.MEAL, title="식사",
    )
    near = _event(
        [(EventSourceType.STAY, morning.raw_id)], start=_at(14, 50), end=_at(15, 0), title="바로 뒤",
    )
    alone = _event([(EventSourceType.STAY, morning.raw_id)], start=_at(22), end=_at(22, 30), title="근거가 이것뿐")

    settle_event_evidence(_draft(meal, near, alone), request, clues)

    assert [r.raw_id for r in meal.source_refs] == [evening.raw_id]
    assert [r.raw_id for r in near.source_refs] == [morning.raw_id], "앞뒤 20분은 그 시간의 기록이다."
    assert [r.raw_id for r in alone.source_refs] == [morning.raw_id]



def _stay_day(*notifications):
    from app.services.skeleton_guard import settle_event_evidence
    from app.services.text_suggestions import suggest_text_fixes

    stay = stay_item("day", start="2026-06-20T10:57:00", end="2026-06-20T17:45:00")
    request = make_request(stays=[stay], notifications=list(notifications))
    clues = _clues(request)
    base = next(bundle.base_event for bundle in clues.bundles if bundle.base_event)

    def settle(*events):
        draft = _draft(*events)
        settle_event_evidence(draft, request, clues)
        return draft

    settle.suggest = lambda draft: suggest_text_fixes(draft, request, clues)
    return stay, base, settle


def test_a_stay_with_only_location_evidence_is_handed_to_repair():
    """Repair 가 7시간 체류를 안쪽 점심과 같은 문장으로 고쳐 쓴 실제 출력. 코드는 고치지 않고 제안만 넘긴다."""

    stay, base, settle = _stay_day()
    outer = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title="점심 식사",
        description="{place}에서 점심을 먹었다.", base=[base.base_id],
    )

    draft = settle(outer)
    [(event, suggestion)] = settle.suggest(draft)

    assert event is outer
    assert suggestion["title"] == base.title and "시간을 보냈다" in suggestion["description"]
    assert (outer.title, outer.description) == ("점심 식사", "{place}에서 점심을 먹었다."), "글은 코드가 바꾸지 않는다."


def test_a_stay_keeps_a_sentence_that_only_says_where():
    stay, base, settle = _stay_day()
    night = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title="지범로에서 보낸 밤",
        description="{place}에서 밤을 보냈다.", base=[base.base_id],
    )

    settle(night)

    assert night.title == "지범로에서 보낸 밤"


def test_a_notification_stays_on_the_event_that_took_it_out():
    """같은 대화 알림이 체류와 대화 event 둘 다에 있고 체류 문장이 대화로 덮인 실제 출력."""

    message = notification_item("n", "Threads", "OO", posted="2026-06-20T11:06:00")
    stay, base, settle = _stay_day(message)
    outer = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, message.raw_id)],
        start=_at(10, 57), end=_at(17, 45), title="포트폴리오 이야기", description="OO와 포트폴리오를 이야기했다.",
        base=[base.base_id],
    )
    talk = _event(
        [(EventSourceType.NOTIFICATION, message.raw_id)], start=_at(11, 6), end=_at(11, 6),
        event_type=EventType.SOCIAL, title="OO와 대화", description="OO와 포트폴리오를 이야기했다.",
    )

    draft = settle(outer, talk)

    assert [r.raw_id for r in outer.source_refs] == [stay.raw_id]
    assert [r.raw_id for r in talk.source_refs] == [message.raw_id]
    suggestions = {id(event): suggestion for event, suggestion in settle.suggest(draft)}
    assert suggestions[id(outer)]["title"] == base.title, "알림이 빠지면 체류의 글은 Repair 가 고친다."


def test_a_notification_that_is_the_only_evidence_stays_where_it_is():
    """근거를 모두 잃는 event 를 만들지 않는다. 지우는 것은 다른 단계의 몫이다."""

    message = notification_item("n", "Threads", "OO", posted="2026-06-20T11:06:00")
    _, _, settle = _stay_day(message)
    first = _event([(EventSourceType.NOTIFICATION, message.raw_id)], start=_at(11, 6), end=_at(11, 6), title="가")
    second = _event([(EventSourceType.NOTIFICATION, message.raw_id)], start=_at(11), end=_at(12), title="나")

    settle(first, second)

    assert [r.raw_id for r in second.source_refs] == [message.raw_id]


def test_a_stay_covered_by_an_activity_from_the_same_base_joins_it():
    """사진 하나로 체류 전체를 덮은 event 옆에 같은 시간의 `보낸 시간` 이 나란히 남은 실제 출력."""

    stay, base, settle = _stay_day()
    rest = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title="보낸 시간",
        description="{place}에서 시간을 보냈다.", base=[base.base_id],
    )
    social = _event(
        [(EventSourceType.PHOTO, fixture_raw_id("photo"))], start=_at(10, 57), end=_at(17, 45),
        event_type=EventType.SOCIAL, title="두 사람과 시간", base=[base.base_id],
    )

    draft = settle(rest, social)

    assert draft.events == [social]
    assert stay.raw_id in [r.raw_id for r in social.source_refs]


def test_a_stay_elsewhere_inside_a_long_calendar_stays():
    """일정 중 잠깐 다른 장소에 간 체류는 뼈대로 남는다(사용자 결정). 기본 event 가 달라 합치지 않는다."""

    stay, base, settle = _stay_day()
    cafe = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(11), end=_at(12), title="보낸 시간",
        description="{place}에서 시간을 보냈다.", base=[base.base_id],
    )
    meeting = _event(
        [(EventSourceType.CALENDAR, fixture_raw_id("calendar"))], start=_at(9), end=_at(23),
        event_type=EventType.MEETING, title="회의", base=["E9"],
    )

    draft = settle(cafe, meeting)

    assert draft.events == [cafe, meeting]



def _notification_clue(label, kind, *, today=None, title="Amazon 결제", at=None):
    from app.schemas.clues import NotificationClue

    return NotificationClue(
        id=label, kind=kind, rawIds=[fixture_raw_id(label)], title=title, summary="Amazon에서 결제했다.",
        scheduleToday=today, firstAt=at or _at(14, 23), lastAt=at or _at(14, 23),
    )


def _without_location(*clues):
    from app.services.skeleton_guard import ensure_notification_events

    request = make_request()
    bundle = _clues(request, clues)
    return lambda *events: ensure_notification_events(draft := _draft(*events), request, bundle) or draft


def test_a_day_without_location_keeps_its_payment_as_an_event():
    """위치가 없는 날은 알림이 그날의 일이다. 붙일 곳이 없다고 버린 결제를 되살린다."""

    clue = _notification_clue("N1", NotificationClueKind.PAYMENT)

    [event] = _without_location(clue)().events

    assert (event.title, event.description) == ("Amazon 결제", "Amazon에서 결제했다.")
    assert event.start_time == event.end_time == _at(14, 23)
    assert [r.raw_id for r in event.source_refs] == clue.raw_ids


def test_an_alert_already_on_an_event_is_not_restored():
    clue = _notification_clue("N1", NotificationClueKind.PAYMENT)
    meal = _event(
        [(EventSourceType.NOTIFICATION, clue.raw_ids[0])], start=_at(14), end=_at(14, 30), event_type=EventType.MEAL,
    )

    assert _without_location(clue)(meal).events == [meal]


def test_only_what_the_user_did_comes_back():
    """대화·소식·광고는 코드가 고르지 않는다. 다른 날의 예약과 날짜를 모르는 예약은 오늘 일이 아니다."""

    clues = [
        _notification_clue("N1", NotificationClueKind.CONVERSATION),
        _notification_clue("N2", NotificationClueKind.NEWS),
        _notification_clue("N3", NotificationClueKind.PROMOTION),
        _notification_clue("N4", NotificationClueKind.RESERVATION, today=False),
        _notification_clue("N5", NotificationClueKind.RESERVATION),
        _notification_clue("N6", NotificationClueKind.PAYMENT, today=False),
    ]

    photo = _event(
        [(EventSourceType.PHOTO, fixture_raw_id("p"))], start=_at(12), end=_at(12), event_type=EventType.PHOTO_MOMENT,
    )

    assert _without_location(*clues)(photo).events == [photo]


def test_a_reservation_for_today_and_a_result_come_back():
    clues = [
        _notification_clue("N1", NotificationClueKind.RESERVATION, today=True),
        _notification_clue("N2", NotificationClueKind.ACTION_RESULT),
    ]

    assert len(_without_location(*clues)().events) == 2


def test_an_alert_during_sleep_stays_out():
    """수면과 겹친 event 는 확정이 지운다. 되살리면 다음 확정과 번갈아 생겼다 사라진다."""

    sleep = _event(
        [(EventSourceType.CALENDAR, fixture_raw_id("sleep"))], start=_at(0), end=_at(8), event_type=EventType.SLEEP,
    )
    clue = _notification_clue("N1", NotificationClueKind.PAYMENT, at=_at(3, 55))

    assert _without_location(clue)(sleep).events == [sleep]


def test_a_located_day_leaves_alerts_to_timeline():
    from app.services.skeleton_guard import ensure_notification_events

    stay = stay_item("day", start="2026-06-20T10:57:00", end="2026-06-20T17:45:00")
    request = make_request(stays=[stay])
    clue = _notification_clue("N1", NotificationClueKind.PAYMENT)
    frame = _event([(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45))
    draft = _draft(frame)

    ensure_notification_events(draft, request, _clues(request, [clue]))

    assert draft.events == [frame]



def test_a_stay_cannot_slip_an_activity_into_the_place_only_sentence():
    """`보냈다` 로 끝나도 활동을 말하면 틀 문장이 아니다. 위치뿐인 7시간 체류에 음식 이름이 붙은 실제 출력."""

    stay, base, settle = _stay_day()
    outer = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title="대현동에서 보낸 시간",
        description="{place}에서 순두부찌개를 먹으며 시간을 보냈다.", base=[base.base_id],
    )

    [(_, suggestion)] = settle.suggest(settle(outer))

    assert "순두부찌개" not in suggestion["description"]


def test_a_restored_alert_goes_through_confirmation_and_the_result_contract():
    """위치 없는 날 되살린 결제가 확정의 나머지 단계에서 지워지지 않고 결과 저장 계약으로 나간다."""

    from app.services.draft_repair import repair_draft
    from app.services.timeline_result import build_result_request

    message = notification_item("pay", "토스", "결제", posted="2026-06-20T14:23:00")
    request = make_request(notifications=[message])
    clue = _notification_clue("pay", NotificationClueKind.PAYMENT)
    clue.raw_ids = [message.raw_id]
    clues = _clues(request, [clue])

    draft = repair_draft(_draft(), request, clues=clues)

    [restored] = [event for event in draft.events if event.title == "Amazon 결제"]
    assert restored.start_time == restored.end_time == _at(14, 23)
    assert restored.client_event_id.startswith("event-")
    body = build_result_request(draft, request).model_dump(by_alias=True, mode="json")
    assert [message.raw_id] in [event["sourceRawIds"] for event in body["events"]]

    twice = repair_draft(draft, request, clues=clues)

    assert len([event for event in twice.events if event.title == "Amazon 결제"]) == 1, "확정이 반복돼도 한 번만 되살린다."



def test_a_stay_with_no_calendar_is_not_sleep():
    """일정 없는 저녁 체류가 `잠` 이 된 실제 출력. 위치는 어디에 있었는지만 말한다."""

    stay, base, settle = _stay_day()
    evening = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), event_type=EventType.SLEEP,
        title="지범로에서 잠", description="{place}에서 잠을 잤다.", base=[base.base_id],
    )

    [(_, suggestion)] = settle.suggest(settle(evening))

    assert (suggestion["eventType"], suggestion["title"]) == ("STAY", base.title)
    assert evening.event_type is EventType.SLEEP


def test_same_titles_in_a_row_go_to_repair():
    """같은 일이면 괜찮지만(사용자 결정) 다른 음식이 같은 제목으로 이어진 실제 출력이 있어 Repair 가 판단한다."""

    from app.services.confirm_report import ConfirmReport
    from app.services.draft_repair import repair_draft

    first, second = photo_item("a", taken="2026-06-20T12:00:00"), photo_item("b", taken="2026-06-20T13:00:00")
    request = make_request(photos=[first, second])
    report = ConfirmReport()

    repair_draft(
        _draft(
            _event([(EventSourceType.PHOTO, first.raw_id)], start=_at(12), end=_at(12, 30), event_type=EventType.MEAL, title="식사"),
            _event([(EventSourceType.PHOTO, second.raw_id)], start=_at(13), end=_at(13, 30), event_type=EventType.MEAL, title="간식"),
        ),
        request,
        report=report,
        clues=_clues(request),
    )
    assert not [f for f in report.findings if f["kind"] == "SAME_TITLE_IN_A_ROW"]

    report = ConfirmReport()
    draft = repair_draft(
        _draft(
            _event([(EventSourceType.PHOTO, first.raw_id)], start=_at(12), end=_at(12, 30), event_type=EventType.MEAL, title="식사"),
            _event([(EventSourceType.PHOTO, second.raw_id)], start=_at(13), end=_at(13, 30), event_type=EventType.MEAL, title="식사"),
        ),
        request,
        report=report,
        clues=_clues(request),
    )

    [found] = [f for f in report.findings if f["kind"] == "SAME_TITLE_IN_A_ROW"]
    assert found["clientEventId"] == draft.events[0].client_event_id
    assert found["nextEvent"] == draft.events[1].client_event_id



@pytest.mark.parametrize(
    "before, after",
    [
        ("{place}에서 점심을 먹었다.", "{place}에서 점심을 먹고 시간을 보냈다."),
        ("{place}에서 음식을 먹으며 시간을 보냈다.", "{place}에서 음식을 먹고 시간을 보냈다."),
        ("{place}에서 식사했다.", "{place}에서 식사하고 시간을 보냈다."),
        ("{place}에서 식사하며 다른 사람과 시간을 보냈다.", "{place}에서 식사하고 다른 사람과 시간을 보냈다."),
        ("{place}에서 점심을 먹고 시간을 보냈다.", "{place}에서 점심을 먹고 시간을 보냈다."),
    ],
)
def test_a_long_stay_with_a_meal_photo_says_it_ate_and_spent_the_time(before, after):
    """긴 체류에 사진을 넣는 것은 좋지만 문장은 `점심을 먹고 시간을 보냈다` 다(사용자 결정 2026-10-31)."""

    from app.services.meal_guard import word_long_stay_meals

    stay = _event(
        [(EventSourceType.STAY, fixture_raw_id("s")), (EventSourceType.PHOTO, fixture_raw_id("p"))],
        start=_at(10, 57), end=_at(17, 45), description=before,
    )

    word_long_stay_meals(_draft(stay))

    assert stay.description == after


@pytest.mark.parametrize(
    "event_type, start, end, source",
    [
        (EventType.MEAL, _at(12), _at(14), EventSourceType.PHOTO),  # 식당에 머문 식사는 그 시간이 식사다(#163)
        (EventType.SOCIAL, _at(12), _at(13), EventSourceType.PHOTO),  # 60분이면 그 일이 식사다
        (EventType.MEETING, _at(12), _at(18), EventSourceType.CALENDAR),  # 일정 event 는 일정의 문장이다
    ],
)
def test_a_meal_sentence_stays_where_the_time_is_the_meal(event_type, start, end, source):
    from app.services.meal_guard import word_long_stay_meals

    event = _event(
        [(source, fixture_raw_id("x"))], start=start, end=end, event_type=event_type,
        description="{place}에서 점심을 먹었다.",
    )

    word_long_stay_meals(_draft(event))

    assert event.description == "{place}에서 점심을 먹었다."


def test_confirmation_hands_text_rules_to_repair_and_leaves_the_text_alone():
    """확정은 글을 고치지 않는다. 규칙을 어긴 글은 `TEXT_CORRECTION` 으로 Repair 에 넘긴다(사용자 결정 2026-10-31)."""

    from app.services.confirm_report import ConfirmReport
    from app.services.draft_repair import repair_draft

    stay = stay_item("day", start="2026-06-20T10:57:00", end="2026-06-20T17:45:00")
    photo = photo_item("lunch", taken="2026-06-20T12:49:00")
    request = make_request(stays=[stay], photos=[photo])
    clues = _clues(request)
    base = next(bundle.base_event for bundle in clues.bundles if bundle.base_event)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.PHOTO, photo.raw_id)],
        start=_at(10, 57), end=_at(17, 45), title="대현동에서 점심", description="{place}에서 점심을 먹었다.",
        base=[base.base_id],
    )
    report = ConfirmReport()

    draft = repair_draft(_draft(event), request, report=report, clues=clues)

    assert draft.events[0].description == "{place}에서 점심을 먹었다."
    [found] = [f for f in report.findings if f["kind"] == "TEXT_CORRECTION"]
    assert found["description"].endswith("점심을 먹고 시간을 보냈다.")
    assert any("긴 체류" in rule for rule in found["rules"])


def test_an_event_that_gave_its_photo_away_says_only_where():
    """사진을 식사 event 에 넘겨주고 체류만 남은 `SOCIAL` 이 `OO와 함께했다` 로 남은 실제 출력."""

    stay, base, settle = _stay_day()
    social = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), event_type=EventType.SOCIAL,
        title="함께한 시간", description="{place}에서 OO와 함께했다.", base=[base.base_id],
    )

    [(_, suggestion)] = settle.suggest(settle(social))

    assert (suggestion["eventType"], suggestion["title"]) == ("STAY", base.title)


@pytest.mark.parametrize("event_type", [EventType.SHOPPING, EventType.CULTURE, EventType.ERRAND, EventType.UNKNOWN])
def test_a_type_that_only_a_stay_supports_goes_back_to_stay(event_type):
    """쇼핑몰·영화관·병원에 머문 것만으로는 그 일을 한 것이 아니다(#150, #172). 코드는 제안만 넘긴다."""

    stay, base, settle = _stay_day()
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), event_type=event_type,
        title="쇼핑", description="{place}에서 쇼핑했다.", base=[base.base_id],
    )

    [(_, suggestion)] = settle.suggest(settle(event))

    assert (suggestion["eventType"], suggestion["title"]) == ("STAY", base.title)
    assert event.event_type is event_type


def test_a_stay_in_the_frame_sentence_needs_no_correction():
    stay, base, settle = _stay_day()
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), event_type=EventType.STAY,
        title=base.title, description=base.description, base=[base.base_id],
    )

    assert not [s for _, s in settle.suggest(settle(event)) if "eventType" in s or "title" in s]


@pytest.mark.parametrize(("today", "corrected"), [(False, True), (True, False), (None, False)])
def test_a_reservation_for_another_day_is_not_what_happened_at_the_stay(today, corrected):
    """다음 주 예매 알림이 온 영화관 체류를 관람으로 쓴 실제 재실행 출력. 그 알림이 근거로 붙어 위치뿐이 아닌
    것으로 보여 빠져나갔다(#172). 예매가 오늘이면 관람의 근거다."""

    from app.services.text_suggestions import suggest_text_fixes

    booking = notification_item("next", "롯데시네마", "예매 완료", posted="2026-06-20T14:00:00")
    stay = stay_item("day", start="2026-06-20T10:57:00", end="2026-06-20T17:45:00")
    request = make_request(stays=[stay], notifications=[booking])
    clue = NotificationClue(
        clue_id="N1", kind=NotificationClueKind.RESERVATION, summary="예매했다.", raw_ids=[booking.raw_id],
        schedule_today=today,
    )
    clues = _clues(request, [clue])
    base = next(bundle.base_event for bundle in clues.bundles if bundle.base_event)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, booking.raw_id)],
        start=_at(10, 57), end=_at(17, 45), event_type=EventType.CULTURE,
        title="영화 관람", description="{place}에서 영화를 봤다.", base=[base.base_id],
    )

    found = [s for _, s in suggest_text_fixes(_draft(event), request, clues) if s.get("eventType") == "STAY"]

    assert bool(found) is corrected


def test_a_shopping_event_with_a_payment_keeps_its_type():
    """결제 알림이 쇼핑을 말하면 체류가 있어도 틀로 되돌리지 않는다."""

    payment = notification_item("pay", "쿠팡", "결제 완료", posted="2026-06-20T14:00:00")
    stay, base, settle = _stay_day(payment)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, payment.raw_id)],
        start=_at(10, 57), end=_at(17, 45), event_type=EventType.SHOPPING,
        title="쇼핑", description="{place}에서 쇼핑했다.", base=[base.base_id],
    )

    assert not [s for _, s in settle.suggest(settle(event)) if "eventType" in s or "title" in s]


def test_twins_on_the_same_stay_become_one():
    """같은 체류를 같은 시간으로 가진 두 event 는 하나만 남는다. 활동 근거가 있는 쪽이 남는다."""

    message = notification_item("n", "YouTube", "답글", posted="2026-06-20T16:23:00")
    stay, base, settle = _stay_day(message)
    rest = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, message.raw_id)],
        start=_at(10, 57), end=_at(17, 45), title="보낸 시간", description="{place}에서 시간을 보냈다.", base=[base.base_id],
    )
    social = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), event_type=EventType.SOCIAL,
        title="함께한 시간", description="{place}에서 OO와 함께했다.", base=[base.base_id],
    )

    assert settle(rest, social).events == [rest]


def test_two_place_only_twins_do_not_absorb_each_other():
    stay, base, settle = _stay_day()
    twins = [
        _event(
            [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title=f"보낸 시간 {label}",
            description="{place}에서 시간을 보냈다.", base=[base.base_id],
        )
        for label in ("가", "나")
    ]

    assert len(settle(*twins).events) == 1



def test_a_restored_photo_keeps_what_the_photo_shows():
    """`사진을 남겼다` 로는 사진의 내용이 사라진다. Repair 가 쓰기 전까지 사진 단서의 말을 둔다."""

    from app.schemas.clues import PhotoClue
    from app.services.photo_guard import CREATED_PHOTO_TITLE
    from app.services.skeleton_guard import NEEDS_WRITING, hand_restored_events_to_repair

    photo = photo_item("lunch", taken="2026-06-20T12:49:00")
    request = make_request(photos=[photo])
    clue = PhotoClue(
        id="P1", rawId=photo.raw_id, takenAt=_at(12, 49), activity="매운 국물 요리와 만두를 먹었다",
    )
    clues = build_timeline_clues(build_location_skeleton(request), [], [clue], [])
    event = _event(
        [(EventSourceType.PHOTO, photo.raw_id)], start=_at(12, 49), end=_at(12, 49),
        event_type=EventType.PHOTO_MOMENT, title=CREATED_PHOTO_TITLE, description="사진을 남겼다.",
    )

    hand_restored_events_to_repair(_draft(event), request, clues)

    assert event.description == "매운 국물 요리와 만두를 먹었다."
    assert event.merge_reason == NEEDS_WRITING


def test_a_restored_calendar_carries_no_code_sentence():
    from app.services.calendar_guard import RESTORED_CALENDAR_DESCRIPTION
    from app.services.skeleton_guard import NEEDS_WRITING, hand_restored_events_to_repair

    request = make_request()
    event = _event(
        [(EventSourceType.CALENDAR, fixture_raw_id("c"))], start=_at(12), end=_at(13),
        event_type=EventType.CALENDAR_EVENT, title="팀 회의", description=RESTORED_CALENDAR_DESCRIPTION,
    )

    hand_restored_events_to_repair(_draft(event), request, _clues(request))

    assert (event.title, event.description, event.merge_reason) == ("팀 회의", "", NEEDS_WRITING)


def test_a_slot_with_no_place_never_reaches_app_server():
    """칸만 남으면 App Server 가 하루치를 400 으로 거절한다."""

    from app.services.timeline_result import build_result_request

    photo = photo_item("p", taken="2026-06-20T12:00:00")
    request = make_request(photos=[photo])
    event = _event(
        [(EventSourceType.PHOTO, photo.raw_id)], start=_at(12), end=_at(12, 30), description="{place}에서 시간을 보냈다.",
    )

    [body] = build_result_request(_draft(event), request).model_dump(by_alias=True, mode="json")["events"]

    assert "{place}" not in body["subtitle"]


def test_an_empty_slot_is_filled_with_the_area_not_the_whole_address():
    """후보가 없는 체류가 `대구 북구 산격동 1346에서 시간을 보냈다.` 로 나간 실제 출력. 번지를 넣지 않는다."""

    from app.services.timeline_result import build_result_request

    stay = stay_item(
        "campus", start="2026-06-20T10:57:00", end="2026-06-20T11:27:00", places=[], address="대구 북구 산격동 1346",
    )
    request = make_request(stays=[stay])
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(11, 27), description="{place}에서 시간을 보냈다.",
    )
    event.address = stay.address

    [body] = build_result_request(_draft(event), request).model_dump(by_alias=True, mode="json")["events"]

    assert body["subtitle"] == "북구 산격동에서 시간을 보냈다."
    assert body["address"] == "대구 북구 산격동 1346", "`address` 필드는 저장용이라 원문 그대로다."


def test_an_empty_slot_loses_the_slot_when_the_address_has_no_area():
    from app.services.place_slots import without_place_slot

    assert without_place_slot("{place}에서 시간을 보냈다.", None) == "시간을 보냈다."


def test_suggestions_leave_the_draft_alone():
    stay, base, settle = _stay_day()
    outer = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(10, 57), end=_at(17, 45), title="점심 식사",
        description="{place}에서 점심을 먹었다.", base=[base.base_id],
    )
    draft = settle(outer)
    before = draft.model_dump()

    settle.suggest(draft)

    assert draft.model_dump() == before



def test_confirmation_keeps_place_names_in_the_slot_only():
    """문장의 장소 이름은 `{place}` 칸으로, 제목과 이동 글에는 장소 이름이 남지 않는다(사용자 결정 2026-10-31)."""

    from app.services.confirm_report import ConfirmReport
    from app.services.draft_repair import repair_draft
    from tests.fixtures.requests import movement_item

    office = stay_item(
        "office", start="2026-06-20T09:00:00", end="2026-06-20T12:00:00", places=["프롬프트 일청담점"],
        address="대구 북구 대현동 25",
    )
    home = stay_item(
        "home", start="2026-06-20T13:00:00", end="2026-06-20T18:00:00", lat=37.6, lon=127.1, places=["한빛아파트"],
        address="대구 수성구 지범로 1",
    )
    move = movement_item("m", start="2026-06-20T12:00:00", end="2026-06-20T13:00:00")
    request = make_request(stays=[office, home], movements=[move])
    clues = _clues(request)
    base = next(bundle.base_event for bundle in clues.bundles if bundle.base_event)
    stay = _event(
        [(EventSourceType.STAY, office.raw_id)], start=_at(9), end=_at(12), title="프롬프트 일청담점에서 점심",
        description="프롬프트 일청담점에서 시간을 보냈다.", base=[base.base_id],
    )
    moving = _event(
        [(EventSourceType.MOVEMENT, move.raw_id)], start=_at(12), end=_at(13), event_type=EventType.MOVEMENT,
        title="이동", description="한빛아파트로 이동했다.",
    )

    draft = repair_draft(_draft(stay, moving), request, report=ConfirmReport(), clues=clues)

    held = next(event for event in draft.events if office.raw_id in [ref.raw_id for ref in event.source_refs])
    assert held.description == "{place}에서 시간을 보냈다."
    assert "프롬프트 일청담점" not in held.title and held.title.endswith("점심")
    [movement] = [event for event in draft.events if event.event_type is EventType.MOVEMENT]
    assert "한빛아파트" not in movement.description



def test_a_day_with_only_talk_and_news_is_not_left_empty():
    """사진도 일정도 결제도 없는 날은 남은 알림이라도 기록이다. event 0건은 정상 결과로 저장돼 아무것도 안 보인다."""

    clues = [
        _notification_clue("N1", NotificationClueKind.CONVERSATION, title="OO와 대화"),
        _notification_clue("N2", NotificationClueKind.NEWS, title="서비스 안내"),
        _notification_clue("N3", NotificationClueKind.PROMOTION, title="광고"),
    ]

    events = _without_location(*clues)().events

    assert [event.title for event in events] == ["OO와 대화", "서비스 안내"]


def test_talk_is_left_to_timeline_when_the_day_already_has_events():
    photo = _event(
        [(EventSourceType.PHOTO, fixture_raw_id("p"))], start=_at(12), end=_at(12), event_type=EventType.PHOTO_MOMENT,
    )
    clue = _notification_clue("N1", NotificationClueKind.CONVERSATION)

    assert _without_location(clue)(photo).events == [photo]


def test_an_empty_day_restores_no_more_than_three_talks():
    clues = [_notification_clue(f"N{index}", NotificationClueKind.CONVERSATION) for index in range(5)]

    assert len(_without_location(*clues)().events) == 3



def _title_day():
    stay = stay_item(
        "day", start="2026-06-20T09:00:00", end="2026-06-20T12:00:00", places=["공덕 SK 리더스뷰"],
        address="서울특별시 마포구 도화동 17-13",
    )
    request = make_request(stays=[stay])
    return stay, request, _clues(request)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("{place}에서 점심", "마포구 도화동에서 점심"),
        ("{place} 점심", "마포구 도화동 점심"),
        ("공덕 SK 리더스뷰에서 매운 국물 요리와 만두", "마포구 도화동에서 매운 국물 요리와 만두"),
        ("공덕 SK 리더스뷰 점심", "점심"),
        ("마포구 도화동에서 점심", "마포구 도화동에서 점심"),
        ("회의에서 나온 이야기", "회의에서 나온 이야기"),
    ],
)
def test_a_title_carries_only_the_address(title, expected):
    """제목에는 `{place}` 도 장소 이름도 남지 않는다(사용자 결정 2026-10-31). 그 밖의 말은 그대로다."""

    from app.services.place_slots import keep_place_names_out

    stay, request, clues = _title_day()
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(12), title=title,
        description="{place}에서 시간을 보냈다.",
    )
    event.address = stay.address

    keep_place_names_out(_draft(event), request, clues)

    assert event.title == expected


def test_a_title_with_no_address_only_loses_the_name():
    """주소 없는 17:24 사진의 제목에 다른 곳의 장소 이름이 붙은 실제 출력. 빌려 올 지명이 없으면 이름만 뺀다."""

    from app.services.place_slots import keep_place_names_out

    stay, request, clues = _title_day()
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(12),
        title="공덕 SK 리더스뷰에서 매운 국물 면 요리와 만두", description="매운 국물 면 요리와 만두를 먹었다.",
    )

    keep_place_names_out(_draft(event), request, clues)

    assert event.title == "매운 국물 면 요리와 만두"
    assert event.description == "매운 국물 면 요리와 만두를 먹었다.", "문장은 건드리지 않는다."


def test_a_movement_carries_only_the_address():
    """이동은 제목과 문장 모두 주소 지명만 나온다. 장소 이름이 없는 글은 그대로 둔다."""

    from app.services.place_slots import keep_place_names_out
    from tests.fixtures.requests import movement_item

    stay = stay_item(
        "home", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00", places=["다른 이름"],
        address="서울특별시 마포구 도화동 17-13",
    )
    move = movement_item("m", start="2026-06-20T10:00:00", end="2026-06-20T11:00:00")
    move.start = GeoPlace(latitude=37.5, longitude=127.0, address="서울특별시 마포구 도화동 17-13", places=["한빛아파트"])
    move.end = GeoPlace(latitude=37.6, longitude=127.1, address="서울특별시 강남구 강남대로 396", places=["OO타워"])
    request = make_request(stays=[stay], movements=[move])
    clues = _clues(request)
    named = _event(
        [(EventSourceType.MOVEMENT, move.raw_id)], start=_at(10), end=_at(11), event_type=EventType.MOVEMENT,
        title="한빛아파트에서 OO타워로 이동", description="{place}에서 OO타워로 이동했다.",
    )

    keep_place_names_out(_draft(named), request, clues)

    for text in (named.title, named.description):
        assert "한빛아파트" not in text and "OO타워" not in text and "{place}" not in text
        assert "마포구" in text and "강남구" in text


# --- 알림은 틀의 근거다 (#173) ---------------------------------------------------------------


def _alert_day(*labels_and_kinds, **request_fields):
    """알림 하나마다 그 시각의 알림뿐인 event 를 만든다."""

    items, clues = [], []
    for number, (label, kind) in enumerate(labels_and_kinds):
        item = notification_item(label, "토스", label, posted=f"2026-06-20T1{number}:00:00")
        clue = _notification_clue(label, kind, at=_at(10 + number))
        clue.raw_ids = [item.raw_id]
        items.append(item)
        clues.append(clue)
    request = make_request(notifications=items, **request_fields)
    events = [
        _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(10 + number), end=_at(10 + number), title=item.title)
        for number, item in enumerate(items)
    ]
    return request, clues, events


def _titles(draft):
    return sorted(event.title for event in draft.events)


def test_with_a_frame_only_conversations_stay_as_alert_events():
    """결제·예약·완료·소식 모두 단독 event 가 되지 않는다(사용자 결정 2026-10-31)."""

    from app.services.skeleton_guard import remove_notification_events

    photo = photo_item("p", taken="2026-06-20T09:00:00")
    request, clues, events = _alert_day(
        ("chat", NotificationClueKind.CONVERSATION),
        ("pay", NotificationClueKind.PAYMENT),
        ("book", NotificationClueKind.RESERVATION),
        ("done", NotificationClueKind.ACTION_RESULT),
        ("news", NotificationClueKind.NEWS),
        photos=[photo],
    )
    frame = _event([(EventSourceType.PHOTO, photo.raw_id)], start=_at(9), end=_at(9), title="사진")
    draft = _draft(frame, *events)

    remove_notification_events(draft, request, _clues(request, clues))

    assert _titles(draft) == ["chat", "사진"]
    assert "알림으로 만든 event 4건" in draft.warnings[-1].message


def test_a_conversation_with_a_payment_still_counts_as_a_conversation():
    """당근 거래 대화 — 대화에 거래 알림이 함께 붙어도 대화다(#167)."""

    from app.services.skeleton_guard import remove_notification_events

    photo = photo_item("p", taken="2026-06-20T09:00:00")
    request, clues, [chat, pay] = _alert_day(
        ("chat", NotificationClueKind.CONVERSATION), ("pay", NotificationClueKind.PAYMENT), photos=[photo]
    )
    chat.source_refs = [*chat.source_refs, *pay.source_refs]
    frame = _event([(EventSourceType.PHOTO, photo.raw_id)], start=_at(9), end=_at(9), title="사진")
    draft = _draft(frame, chat)

    remove_notification_events(draft, request, _clues(request, clues))

    assert _titles(draft) == ["chat", "사진"]


def test_a_day_without_any_frame_keeps_its_alerts_through_repeated_confirmation():
    """사진도 일정도 위치도 없는 날은 알림이 그날의 기록이다. 소식도 다음 확정에서 지워지지 않는다."""

    request, clues, _ = _alert_day(
        ("chat", NotificationClueKind.CONVERSATION), ("news", NotificationClueKind.NEWS)
    )
    bundle = _clues(request, clues)

    draft = repair_draft(_draft(), request, clues=bundle)
    assert len(draft.events) == 2

    assert len(repair_draft(draft, request, clues=bundle).events) == 2


def test_a_day_with_photos_does_not_restore_a_payment():
    """위치가 없어도 사진·일정이 틀이다. Timeline 이 만든 결제 event 도 지워진다."""

    photo = photo_item("p", taken="2026-06-20T09:00:00")
    request, clues, [pay] = _alert_day(("pay", NotificationClueKind.PAYMENT), photos=[photo])
    frame = _event(
        [(EventSourceType.PHOTO, photo.raw_id)], start=_at(9), end=_at(9), event_type=EventType.PHOTO_MOMENT, title="사진",
    )

    draft = repair_draft(_draft(frame, pay), request, clues=_clues(request, clues))

    assert _titles(draft) == ["사진"]


def _stay_with_alert(kind, event_type=EventType.UNKNOWN):
    stay = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00")
    item = notification_item("alert", "토스", "알림", posted="2026-06-20T12:30:00")
    clue = _notification_clue("N1", kind, at=_at(12, 30))
    clue.raw_ids = [item.raw_id]
    request = make_request(stays=[stay], notifications=[item])
    whole = _event([(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(18), title="체류", base=("E1",))
    piece = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(12, 30), end=_at(13), event_type=event_type, title="꺼낸 event", base=("E1",),
    )
    return request, _clues(request, [clue]), whole, piece, item


def test_an_alert_event_pulled_out_of_a_stay_goes_back_as_its_evidence():
    """`from` 을 적어 위치 근거를 가져도 알림으로 만든 event 다. 근거는 체류 event 로 옮긴다."""

    from app.services.skeleton_guard import remove_notification_events

    request, clues, whole, piece, item = _stay_with_alert(NotificationClueKind.NEWS)
    draft = _draft(whole, piece)

    remove_notification_events(draft, request, clues)

    assert draft.events == [whole]
    assert item.raw_id in [ref.raw_id for ref in whole.source_refs]


def test_a_meal_pulled_out_by_a_payment_stays():
    from app.services.skeleton_guard import remove_notification_events

    request, clues, whole, piece, _ = _stay_with_alert(NotificationClueKind.PAYMENT, EventType.MEAL)
    draft = _draft(whole, piece)

    remove_notification_events(draft, request, clues)

    assert _titles(draft) == ["꺼낸 event", "체류"]


def test_an_alert_outside_the_event_time_is_not_its_evidence():
    """15:28 결제가 18:17 식사의 근거로 붙었다. 받은 시각이 맞아야 그 event 의 근거다."""

    from app.services.skeleton_guard import settle_event_evidence

    stay = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T20:00:00")
    far = notification_item("far", "토스", "결제", posted="2026-06-20T15:28:00")
    near = notification_item("near", "토스", "결제", posted="2026-06-20T18:20:00")
    clues = []
    for item, at in ((far, _at(15, 28)), (near, _at(18, 20))):
        clue = _notification_clue(item.raw_id[:6], NotificationClueKind.PAYMENT, at=at)
        clue.raw_ids = [item.raw_id]
        clues.append(clue)
    request = make_request(stays=[stay], notifications=[far, near])
    meal = _event(
        [
            (EventSourceType.STAY, stay.raw_id),
            (EventSourceType.NOTIFICATION, far.raw_id),
            (EventSourceType.NOTIFICATION, near.raw_id),
        ],
        start=_at(18, 17), end=_at(19), event_type=EventType.MEAL,
    )

    settle_event_evidence(_draft(meal), request, _clues(request, clues))

    assert [ref.raw_id for ref in meal.source_refs] == [stay.raw_id, near.raw_id]


def test_an_alert_far_from_the_stay_it_came_with_leaves_the_event():
    """체류 근거로 시간이 정해진 event 에 다른 시각의 결제가 남지 않는다. 알림뿐인 event 는
    `test_notification_guarantee.py` 가 본다."""

    morning = stay_item("am", lat=37.50, start="2026-06-20T09:00:00", end="2026-06-20T10:00:00")
    evening = stay_item("pm", lat=37.60, start="2026-06-20T13:00:00", end="2026-06-20T18:00:00")
    item = notification_item("pay", "토스", "결제", posted="2026-06-20T20:00:00")
    clue = _notification_clue("N1", NotificationClueKind.PAYMENT, at=_at(20))
    clue.raw_ids = [item.raw_id]
    request = make_request(stays=[morning, evening], notifications=[item])
    stray = _event(
        [(EventSourceType.STAY, morning.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(20), end=_at(20), title="떠 있는 결제",
    )

    draft = repair_draft(_draft(stray), request, clues=_clues(request, [clue]))

    assert item.raw_id not in {ref.raw_id for event in draft.events for ref in event.source_refs}


def test_a_conversation_with_a_short_record_counts_as_a_conversation():
    """대화의 묶음 id 를 적으면 단독 위치 기록이 딸려 온다. 세지 않으면 대화 event 가 3개를 넘는다."""

    short = stay_item("short", start="2026-06-20T19:00:00", end="2026-06-20T19:05:00")
    chat = notification_item("chat", "카카오톡", "친구", posted="2026-06-20T19:02:00")
    request = make_request(stays=[short], notifications=[chat])
    clue = _notification_clue("N1", NotificationClueKind.CONVERSATION, at=_at(19, 2))
    clue.raw_ids = [chat.raw_id]
    clues = _clues(request, [clue])
    assert clues.short_record_ids == {short.raw_id}
    # `SOCIAL` 은 위치 근거가 있어도 대화 event 다(#183). 그 밖의 종류를 단독 기록이 가른다.
    event = _event(
        [(EventSourceType.STAY, short.raw_id), (EventSourceType.NOTIFICATION, chat.raw_id)],
        start=_at(19, 2), end=_at(19, 2), event_type=EventType.WORK, title="업무 대화",
    )
    draft = _draft(event)

    assert conversation_events(draft, request, {chat.raw_id}) == []
    assert conversation_events(draft, request, {chat.raw_id}, aside=clues.short_record_ids) == [event]


# --- v4 에는 UNKNOWN event 가 없다 (#173) -----------------------------------------------------


def test_an_unknown_event_takes_the_type_its_evidence_says():
    from app.services.skeleton_guard import retire_unknown_events

    stay = stay_item("day", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00")
    photo = photo_item("p", taken="2026-06-20T20:00:00")
    chat = notification_item("chat", "카카오톡", "친구", posted="2026-06-20T21:00:00")
    pay = notification_item("pay", "토스", "결제", posted="2026-06-20T21:30:00")
    news = notification_item("news", "메시지", "안내", posted="2026-06-20T22:00:00")
    clues = []
    for item, kind in (
        (chat, NotificationClueKind.CONVERSATION),
        (pay, NotificationClueKind.PAYMENT),
        (news, NotificationClueKind.NEWS),
    ):
        clue = _notification_clue(item.raw_id[:6], kind)
        clue.raw_ids = [item.raw_id]
        clues.append(clue)
    request = make_request(stays=[stay], photos=[photo], notifications=[chat, pay, news])
    events = [
        _event([(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, pay.raw_id)], start=_at(9), end=_at(18)),
        _event([(EventSourceType.PHOTO, photo.raw_id)], start=_at(20), end=_at(20)),
        _event([(EventSourceType.NOTIFICATION, chat.raw_id)], start=_at(21), end=_at(21)),
        _event([(EventSourceType.NOTIFICATION, pay.raw_id)], start=_at(21, 30), end=_at(21, 30)),
        _event([(EventSourceType.NOTIFICATION, news.raw_id)], start=_at(22), end=_at(22)),
        _event([(EventSourceType.CALENDAR, fixture_raw_id("calendar"))], start=_at(23), end=_at(23)),
    ]

    retire_unknown_events(_draft(*events), request, _clues(request, clues))

    assert [event.event_type for event in events] == [
        EventType.STAY,
        EventType.PHOTO_MOMENT,
        EventType.SOCIAL,
        EventType.SHOPPING,
        EventType.ERRAND,
        EventType.CALENDAR_EVENT,
    ]


def test_a_restored_alert_is_not_unknown():
    clue = _notification_clue("N1", NotificationClueKind.PAYMENT)

    [event] = _without_location(clue)().events

    assert event.event_type is EventType.SHOPPING


# --- 일정의 활동 타입 (#184) -----------------------------------------------------------------


#: 일정이 가질 수 있는 타입. `CALENDAR_EVENT` 는 어떤 활동인지 모르는 일정의 자리다.
_CALENDAR_TYPES = (
    EventType.MEETING,
    EventType.CLASS,
    EventType.WORK,
    EventType.EXERCISE,
    EventType.MEAL,
    EventType.SOCIAL,
    EventType.CULTURE,
    EventType.SHOPPING,
    EventType.ERRAND,
    EventType.SLEEP,
    EventType.CALENDAR_EVENT,
)


def _calendar_clues(request, *, photos=(), notifications=()):
    return build_timeline_clues(
        build_location_skeleton(request), build_calendar_clues(request), list(photos), list(notifications)
    )


def _confirm_twice(draft, request, clues):
    """확정 pass 는 Repair 가 돌 때마다 다시 돈다. 두 번 돌아도 같아야 한다."""

    report = _ConfirmReport()
    repair_draft(draft, request, extended=True, clues=clues, report=report)
    repair_draft(draft, request, extended=True, clues=clues, report=report)
    return {finding["kind"] for finding in report.findings}


def _holds(event, item):
    return item.raw_id in {ref.raw_id for ref in event.source_refs}


@pytest.mark.parametrize("event_type", _CALENDAR_TYPES)
def test_a_calendar_alone_keeps_its_type_time_and_source(event_type):
    calendar = calendar_item("plan", "일정", start="2026-06-20T14:00:00", end="2026-06-20T15:30:00")
    request = make_request(calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(14), end=_at(15, 30), event_type=event_type, title="일정", description="일정이 있었다.",
        )
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    [kept] = draft.events
    assert (kept.event_type, kept.start_time, kept.end_time) == (event_type, _at(14), _at(15, 30))
    assert _holds(kept, calendar)


@pytest.mark.parametrize("event_type", _CALENDAR_TYPES)
def test_a_calendar_over_a_stay_keeps_its_type_and_the_stay_as_its_place(event_type):
    stay = stay_item("office", start="2026-06-20T14:00:00", end="2026-06-20T15:30:00", places=["OO타워"], address="서울 마포구 도화동 1")
    calendar = calendar_item("plan", "일정", start="2026-06-20T14:00:00", end="2026-06-20T15:30:00")
    request = make_request(stays=[stay], calendars=[calendar])
    clues = _calendar_clues(request)
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id), (EventSourceType.STAY, stay.raw_id)],
            start=_at(14), end=_at(15, 30), event_type=event_type, title="일정",
            description="{place}에서 일정을 했다.", base=["E1"],
        )
    )

    _confirm_twice(draft, request, clues)

    [kept] = draft.events
    assert (kept.event_type, kept.start_time, kept.end_time) == (event_type, _at(14), _at(15, 30))
    assert _holds(kept, calendar) and _holds(kept, stay)


@pytest.mark.parametrize("event_type", _CALENDAR_TYPES)
def test_a_calendar_with_a_photo_and_a_conversation_nearby_keeps_its_type(event_type):
    stay = stay_item("office", start="2026-06-20T14:00:00", end="2026-06-20T16:00:00", places=["OO타워"], address="서울 마포구 도화동 1")
    calendar = calendar_item("plan", "일정", start="2026-06-20T14:00:00", end="2026-06-20T16:00:00")
    photo = photo_item("board", taken="2026-06-20T14:30:00")
    talk = notification_item("talk", "카카오톡", "민수", posted="2026-06-20T17:00:00")
    request = make_request(stays=[stay], calendars=[calendar], photos=[photo], notifications=[talk])
    clues = _calendar_clues(
        request,
        photos=[PhotoClue(clue_id="P1", raw_id=photo.raw_id, taken_at=_at(14, 30), activity="화이트보드를 봤다")],
        notifications=[
            NotificationClue(
                id="N1", kind=NotificationClueKind.CONVERSATION, rawIds=[talk.raw_id], title="민수",
                summary="저녁 약속 얘기", firstAt=_at(17), lastAt=_at(17),
            )
        ],
    )
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id), (EventSourceType.STAY, stay.raw_id), (EventSourceType.PHOTO, photo.raw_id)],
            start=_at(14), end=_at(16), event_type=event_type, title="일정",
            description="{place}에서 일정을 했다.", base=["E1"],
        ),
        _event(
            [(EventSourceType.NOTIFICATION, talk.raw_id)],
            start=_at(17), end=_at(17), event_type=EventType.SOCIAL, title="민수와 대화", description="민수와 이야기를 나눴다.",
        ),
    )

    _confirm_twice(draft, request, clues)

    calendar_event = next(event for event in draft.events if _holds(event, calendar))
    assert (calendar_event.event_type, calendar_event.start_time, calendar_event.end_time) == (event_type, _at(14), _at(16))
    assert _holds(calendar_event, photo), "사진은 그 일정 event 에 남는다."
    assert any(_holds(event, talk) for event in draft.events), "대화 event 도 남는다."


def test_a_meal_calendar_keeps_its_time_without_a_photo():
    """12:00~13:30 점심 약속을 60분으로 자르고 근거 없는 식사로 짚었다. 일정은 계획이라 확신은 낮게 둔다."""

    calendar = calendar_item("lunch", "팀 점심 식사", start="2026-06-20T12:00:00", end="2026-06-20T13:30:00")
    request = make_request(calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(12), end=_at(13, 30), event_type=EventType.MEAL, title="팀 점심 식사",
            description="팀 점심 식사 일정이 있었다.",
        )
    )

    kinds = _confirm_twice(draft, request, _calendar_clues(request))

    [meal] = draft.events
    assert (meal.start_time, meal.end_time) == (_at(12), _at(13, 30))
    assert meal.confidence <= 0.6
    assert "UNSUPPORTED_MEAL" not in kinds


def test_a_meal_calendar_shorter_than_twenty_minutes_is_not_stretched():
    calendar = calendar_item("snack", "커피 타임", start="2026-06-20T15:00:00", end="2026-06-20T15:15:00")
    request = make_request(calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(15), end=_at(15, 15), event_type=EventType.MEAL, title="커피 타임", description="커피 타임 일정이 있었다.",
        )
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    assert (draft.events[0].start_time, draft.events[0].end_time) == (_at(15), _at(15, 15))


def test_a_meal_inside_a_calendar_still_needs_a_photo_or_a_payment():
    """일정 시간과 다른 식사는 일정이 시간을 정한 것이 아니다. 회의 일정을 근거로 댄 두 시간 식사는 그대로 짧아진다."""

    calendar = calendar_item("day", "팀 회의", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00")
    request = make_request(calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(9), end=_at(18), event_type=EventType.MEETING, title="팀 회의", description="팀 회의 일정이 있었다.",
        ),
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(12), end=_at(14), event_type=EventType.MEAL, title="점심", description="점심을 먹었다.",
        ),
    )

    kinds = _confirm_twice(draft, request, _calendar_clues(request))

    meal = next(event for event in draft.events if event.event_type is EventType.MEAL)
    assert (meal.start_time, meal.end_time) == (_at(12), _at(13))
    assert "UNSUPPORTED_MEAL" in kinds


def test_an_activity_owns_the_calendar_over_a_stay_that_holds_it():
    """일정을 가진 `보낸 시간` 이 주인이 되면 Timeline 이 고른 `MEETING` 이 합쳐져 사라진다."""

    stay = stay_item("office", start="2026-06-20T14:00:00", end="2026-06-20T16:00:00", address="서울 마포구 도화동 1")
    calendar = calendar_item("meeting", "팀 회의", start="2026-06-20T14:00:00", end="2026-06-20T16:00:00")
    request = make_request(stays=[stay], calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id), (EventSourceType.STAY, stay.raw_id)],
            start=_at(14), end=_at(16), event_type=EventType.STAY, title="보낸 시간",
            description="{place}에서 시간을 보냈다.", base=["E1"],
        ),
        _event(
            [(EventSourceType.STAY, stay.raw_id)],
            start=_at(14), end=_at(16), event_type=EventType.MEETING, title="팀 회의",
            description="{place}에서 팀 회의를 했다.", base=["E1"],
        ),
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    [owner] = draft.events
    assert (owner.event_type, owner.title) == (EventType.MEETING, "팀 회의")
    assert _holds(owner, calendar) and _holds(owner, stay)


def _exercise_day():
    home = stay_item("home", lat=37.27, start="2026-06-20T06:00:00", end="2026-06-20T18:00:00", address="서울 마포구 망원동 1")
    gym = stay_item("gym", lat=37.54, start="2026-06-20T19:00:00", end="2026-06-20T20:30:00", address="서울 마포구 도화동 1")
    move = MovementItem(
        raw_id=fixture_raw_id("movement-go"), start_at="2026-06-20T18:00:00", end_at="2026-06-20T19:00:00",
        start=GeoPlace(latitude=37.27, longitude=127.0), end=GeoPlace(latitude=37.54, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=30000,
    )
    return home, gym, move


def test_an_exercise_calendar_gives_up_the_movement_it_borrowed():
    """`EXERCISE` 는 산책 때문에 이동 분리에서 빠져 있었다. 운동 일정이 앞 이동을 근거로 품은 채 남았다."""

    home, gym, move = _exercise_day()
    calendar = calendar_item("pt", "PT 수업", start="2026-06-20T18:30:00", end="2026-06-20T20:30:00")
    request = make_request(stays=[home, gym], movements=[move], calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.STAY, home.raw_id)],
            start=_at(6), end=_at(18), event_type=EventType.STAY, title="집", description="{place}에서 시간을 보냈다.", base=["E1"],
        ),
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id), (EventSourceType.MOVEMENT, move.raw_id), (EventSourceType.STAY, gym.raw_id)],
            start=_at(18, 30), end=_at(20, 30), event_type=EventType.EXERCISE, title="PT 수업",
            description="{place}에서 PT 수업을 했다.", base=["E3"],
        ),
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    exercise = next(event for event in draft.events if event.event_type is EventType.EXERCISE)
    assert (exercise.start_time, exercise.end_time) == (_at(18, 30), _at(20, 30)), "일정 시간은 자르지 않는다."
    assert not _holds(exercise, move)
    assert any(event.event_type is EventType.MOVEMENT and _holds(event, move) for event in draft.events)


def test_an_exercise_calendar_keeps_the_walk_it_is_about():
    """산책 구간을 가진 운동 일정은 산책이 곧 그 운동이라 그대로다."""

    out = MovementItem(
        raw_id=fixture_raw_id("walk-out"), start_at="2026-06-20T19:00:00", end_at="2026-06-20T19:20:00",
        start=GeoPlace(latitude=37.5, longitude=127.0), end=GeoPlace(latitude=37.508, longitude=127.0),
        transports=["WALKING"], distance_meters=800,
    )
    back = MovementItem(
        raw_id=fixture_raw_id("walk-back"), start_at="2026-06-20T19:20:00", end_at="2026-06-20T19:40:00",
        start=GeoPlace(latitude=37.508, longitude=127.0), end=GeoPlace(latitude=37.5, longitude=127.0),
        transports=["WALKING"], distance_meters=800,
    )
    calendar = calendar_item("walk", "저녁 산책", start="2026-06-20T19:00:00", end="2026-06-20T19:40:00")
    request = make_request(movements=[out, back], calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id), (EventSourceType.MOVEMENT, out.raw_id), (EventSourceType.MOVEMENT, back.raw_id)],
            start=_at(19), end=_at(19, 40), event_type=EventType.EXERCISE, title="저녁 산책",
            description="저녁 산책을 했다.", base=["E1"],
        )
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    [walk] = draft.events
    assert walk.event_type is EventType.EXERCISE
    assert _holds(walk, out) and _holds(walk, back)


def _long_work_day(*, all_day=False):
    office = stay_item("office", lat=37.50, start="2026-06-20T09:00:00", end="2026-06-20T18:00:00", places=["OO타워"], address="서울 마포구 도화동 1")
    dinner = stay_item("dinner", lat=37.55, start="2026-06-20T18:30:00", end="2026-06-20T22:00:00", places=["XX식당"], address="서울 마포구 망원동 1")
    if all_day:
        calendar = CalendarItem(
            raw_id=fixture_raw_id("calendar-trip"), start_at="2026-06-20T00:00:00", end_at="2026-06-21T00:00:00",
            title="부산 출장", all_day=True,
        )
    else:
        calendar = calendar_item("dev", "프로젝트 개발", start="2026-06-20T09:00:00", end="2026-06-20T23:00:00")
    return office, dinner, calendar, make_request(stays=[office, dinner], calendars=[calendar])


@pytest.mark.parametrize(("all_day", "start", "end"), [(False, _at(9), _at(23)), (True, _at(0), _at(23, 59))])
def test_a_day_long_calendar_takes_its_type_and_leaves_the_stays_alone(all_day, start, end):
    """길이는 타입을 정할 이유가 아니다(사용자 지적 2026-11-02). 배경 일정의 뜻은 구조가 지킨다."""

    office, dinner, calendar, request = _long_work_day(all_day=all_day)
    clues = _calendar_clues(request)
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=start, end=end, event_type=EventType.WORK, title=calendar.title, description=f"{calendar.title} 일정이 있었다.",
        ),
        _event(
            [(EventSourceType.STAY, office.raw_id)],
            start=_at(9), end=_at(18), event_type=EventType.STAY, title="도화동에서 보낸 시간",
            description="{place}에서 시간을 보냈다.", base=["E1"],
        ),
        _event(
            [(EventSourceType.STAY, dinner.raw_id)],
            start=_at(18, 30), end=_at(22), event_type=EventType.STAY, title="망원동에서 보낸 시간",
            description="{place}에서 시간을 보냈다.", base=["E3"],
        ),
    )

    _confirm_twice(draft, request, clues)

    assert not any(bundle.base_event and bundle.base_event.calendar for bundle in clues.bundles), "체류의 주인이 되지 않는다."
    long_event = next(event for event in draft.events if _holds(event, calendar))
    assert (long_event.event_type, long_event.start_time, long_event.end_time) == (EventType.WORK, start, end)
    assert sum(event.event_type is EventType.STAY for event in draft.events) == 2


def test_work_inside_a_long_work_calendar_stays_when_they_share_a_stay():
    """타입·장소가 같다고 합치자 09:00~23:00 `WORK` 일정 안의 `배포 준비` 가 사라졌다."""

    office, dinner, calendar, request = _long_work_day()
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id), (EventSourceType.STAY, office.raw_id)],
            start=_at(9), end=_at(23), event_type=EventType.WORK, title="프로젝트 개발",
            description="{place}에서 프로젝트 개발 일정이 있었다.",
        ),
        _event(
            [(EventSourceType.STAY, office.raw_id)],
            start=_at(13), end=_at(15), event_type=EventType.WORK, title="배포 준비",
            description="{place}에서 배포를 준비했다.", base=["E1"],
        ),
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    assert {"프로젝트 개발", "배포 준비"} <= {event.title for event in draft.events}


def test_work_inside_a_long_work_calendar_stays_when_they_cite_the_same_calendar():
    """위치가 없는 날에는 같은 일정을 근거로 가졌다는 것이 합치는 이유가 됐다."""

    calendar = calendar_item("dev", "프로젝트 개발", start="2026-06-20T09:00:00", end="2026-06-20T23:00:00")
    request = make_request(calendars=[calendar])
    draft = _draft(
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(9), end=_at(23), event_type=EventType.WORK, title="프로젝트 개발", description="프로젝트 개발 일정이 있었다.",
        ),
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(13), end=_at(15), event_type=EventType.WORK, title="배포 준비", description="배포를 준비했다.",
        ),
    )

    _confirm_twice(draft, request, _calendar_clues(request))

    assert {event.title for event in draft.events} == {"프로젝트 개발", "배포 준비"}


def _same_place_meetings(first, second):
    events = [
        _event(
            [(EventSourceType.CALENDAR, calendar.raw_id)],
            start=_at(14), end=_at(15), event_type=EventType.MEETING, title=title, description="회의 일정이 있었다.",
        )
        for title, calendar in (("첫째", first), ("둘째", second))
    ]
    for event in events:
        event.place = "OO타워"
    return _draft(*events)


def test_two_calendars_of_one_type_and_place_are_two_events():
    first = calendar_item("a", "주간 회의", start="2026-06-20T14:00:00", end="2026-06-20T15:00:00")
    second = calendar_item("b", "면담", start="2026-06-20T14:00:00", end="2026-06-20T15:00:00")
    draft = _same_place_meetings(first, second)

    resolve_overlaps(draft, by_calendar=True)

    assert len(draft.events) == 2


def test_the_same_calendar_written_twice_is_one_event():
    calendar = calendar_item("a", "주간 회의", start="2026-06-20T14:00:00", end="2026-06-20T15:00:00")
    draft = _same_place_meetings(calendar, calendar)

    resolve_overlaps(draft, by_calendar=True)

    assert len(draft.events) == 1


def test_a_restored_calendar_goes_to_repair_to_be_typed():
    """Timeline 이 빠뜨린 일정은 분류 전 표시로 되살아나고 타입은 Repair 가 고른다."""

    calendar = calendar_item("meeting", "팀 회의", start="2026-06-20T14:00:00", end="2026-06-20T16:00:00")
    request = make_request(calendars=[calendar])
    draft = _draft()

    kinds = _confirm_twice(draft, request, _calendar_clues(request))

    [restored] = draft.events
    assert restored.event_type is EventType.CALENDAR_EVENT
    assert "EVENT_TO_WRITE" in kinds


def test_v3_still_cuts_a_meal_calendar_and_merges_events_of_one_type():
    """v3 는 그대로다. 식사 일정 예외와 일정별 중복 판정은 v4 경로에만 있다."""

    lunch = calendar_item("lunch", "팀 점심 식사", start="2026-06-20T12:00:00", end="2026-06-20T13:30:00")
    meal = _event(
        [(EventSourceType.CALENDAR, lunch.raw_id)],
        start=_at(12), end=_at(13, 30), event_type=EventType.MEAL, title="팀 점심 식사", description="점심 약속이 있었다.",
    )
    meals = _draft(meal)
    repair_draft(meals, make_request(calendars=[lunch]), extended=True)
    assert meals.events[0].end_time == _at(13)

    first = calendar_item("a", "주간 회의", start="2026-06-20T14:00:00", end="2026-06-20T15:00:00")
    second = calendar_item("b", "면담", start="2026-06-20T14:00:00", end="2026-06-20T15:00:00")
    draft = _same_place_meetings(first, second)
    resolve_overlaps(draft)
    assert len(draft.events) == 1
