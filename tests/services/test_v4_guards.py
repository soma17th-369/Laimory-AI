"""v4 가 v3 의 강한 규칙을 잃지 않게 하는 확정 검사 (#167, 사용자 결정 2026-10-30).

같은 장면 사진은 event 하나, 캘린더 일정은 빠지지 않고 시간도 바뀌지 않음, 대화 event 하루 3개,
일정 근거 없는 일정 event 금지, 위치 근거 없는 체류 문장 금지, 제목에 장소 금지. 모두 실제
재실행에서 어긴 출력을 그대로 옮긴 것이다.
"""


from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import (
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
from app.services.clue_bundles import build_timeline_clues
from app.services.conversation_guard import enforce_conversation_limit
from app.services.draft_repair import repair_draft
from app.services.location_skeleton import build_location_skeleton
from app.services.photo_guard import merge_photo_scenes
from app.services.place_slots import fix_title_places
from app.services.skeleton_guard import (
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
    assert with_stay.event_type is EventType.UNKNOWN


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
