"""v4 가 v3 의 강한 규칙을 잃지 않게 하는 확정 검사 (#167, 사용자 결정 2026-10-30).

같은 장면 사진은 event 하나, 캘린더 일정은 빠지지 않고 시간도 바뀌지 않음, 대화 event 하루 3개,
일정 근거 없는 일정 event 금지, 위치 근거 없는 체류 문장 금지, 제목에 장소 금지. 모두 실제
재실행에서 어긴 출력을 그대로 옮긴 것이다.
"""

from datetime import datetime, timedelta, timezone

from app.schemas import (
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    MovementItem,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
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
