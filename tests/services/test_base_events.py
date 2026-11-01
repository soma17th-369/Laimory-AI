"""제목 지명과 같은 장소의 주소 (#167, v4, 사용자 결정 2026-10-29).

같은 장소(250m) 묶음마다 코드가 지명 하나(구 + 동, 동이 없으면 구 + 도로명)를 골라 Timeline 입력에
싣는다. 제목은 Timeline 이 쓴다. 도로명 뒤 건물번호는 빼고 도로명 안의 숫자는 둔다.
"""

import pytest

from app.schemas import GeoPlace, MovementItem
from app.services.base_events import AreaNamer, area_namer, sido_of
from app.services.clue_bundles import build_timeline_clues, timeline_input_sections
from app.services.location_skeleton import build_location_skeleton
from app.services.place_resolver import same_place_addresses
from app.services.validator import resolve_timezone
from tests.fixtures.requests import fixture_raw_id, make_request, photo_item, stay_item


@pytest.mark.parametrize(
    ("address", "area"),
    [
        ("서울 마포구 도화동 17-13", "마포구 도화동"),
        ("경기도 용인시 기흥구 동백죽전대로 455-11", "기흥구 동백죽전대로"),
        ("전남광주통합특별시 여수시 덕충동 2079", "여수시 덕충동"),
        ("전남광주통합특별시 여수시 통제영3길 12-15", "여수시 통제영3길"),
        ("서울특별시 관악구 지범로17길 5", "관악구 지범로17길"),
        ("전남광주통합특별시 여수시 소라면 하세동길 17-2", "여수시 소라면"),
    ],
)
def test_area_is_district_and_neighborhood_without_building_numbers(address, area):
    assert AreaNamer().name(address) == area


def test_province_is_added_only_when_the_day_spans_two_of_them():
    addresses = ["경기도 용인시 기흥구 동백죽전대로 455-11", "서울 마포구 도화동 17-13"]

    areas = area_namer(addresses)

    assert areas.multi_region
    assert areas.name(addresses[1]) == "서울 마포구 도화동"
    assert not area_namer(["서울 마포구 도화동 17-13", "서울특별시 마포구 마포대로 89"]).multi_region
    assert sido_of("전남광주통합특별시 여수시 덕충동 2079") == "전남광주"


def _office_day():
    morning = stay_item(
        "morning", lat=37.5435, lon=126.9495, start="2026-06-20T09:00:00", end="2026-06-20T09:30:00",
        address="서울 마포구 도화동 17-13",
    )
    afternoon = stay_item(
        "afternoon", lat=37.5436, lon=126.9496, start="2026-06-20T13:00:00", end="2026-06-20T22:00:00",
        address="서울특별시 마포구 마포대로 83",
    )
    desk = photo_item("desk", taken="2026-06-20T09:20:00", lat=37.5432, lon=126.9498)
    desk.address = "서울특별시 마포구 마포대로 89"
    lunch = stay_item(
        "lunch", lat=37.5470, lon=126.9450, start="2026-06-20T12:00:00", end="2026-06-20T12:40:00",
        address="서울특별시 마포구 백범로 152",
    )
    return make_request(stays=[morning, afternoon, lunch], photos=[desk])


def test_one_place_gets_one_address_and_the_jibun_one_comes_first():
    request = _office_day()

    table = same_place_addresses(request, resolve_timezone(request.timezone))

    assert table["서울특별시 마포구 마포대로 83"] == "서울 마포구 도화동 17-13", (
        "가장 오래 머문 곳의 주소가 도로명이어도 동이 있는 주소(지번)를 고른다."
    )
    assert table["서울특별시 마포구 마포대로 89"] == "서울 마포구 도화동 17-13"
    assert table["서울특별시 마포구 백범로 152"] == "서울특별시 마포구 백범로 152", "250m 넘게 떨어진 곳은 다른 장소다."


def test_timeline_input_gives_one_area_for_one_place():
    request = _office_day()
    table = same_place_addresses(request, resolve_timezone(request.timezone))
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [], same_place=table)

    assert clues.area("서울특별시 마포구 마포대로 89") == "마포구 도화동"
    assert clues.area("서울특별시 마포구 마포대로 83") == "마포구 도화동"
    text = str(timeline_input_sections(clues))
    assert "마포대로" not in text, "같은 장소의 다른 주소는 입력에 싣지 않는다."


def test_base_event_prompt_has_no_title_and_the_restore_template_uses_the_area():
    home = stay_item("home", lat=37.30, start="2026-06-20T06:00:00", end="2026-06-20T08:30:00", address="서울 관악구 대학동 1")
    office = stay_item("office", lat=37.60, start="2026-06-20T09:10:00", end="2026-06-20T18:00:00", address="서울 마포구 도화동 17-13")
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at="2026-06-20T08:30:00", end_at="2026-06-20T09:10:00",
        start=GeoPlace(latitude=37.30, longitude=127.0, address="서울 관악구 대학동 1"),
        end=GeoPlace(latitude=37.60, longitude=127.0, address="서울 마포구 도화동 17-13"),
        transports=["IN_VEHICLE"], distance_meters=30000,
    )
    request = make_request(stays=[home, office], movements=[commute])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])

    bases = [bundle.base_event for bundle in clues.bundles if bundle.base_event]

    assert [base.title for base in bases] == [
        "관악구 대학동에서 시간을 보냄", "관악구 대학동에서 마포구 도화동으로 이동함", "마포구 도화동에서 시간을 보냄",
    ]
    # 이동 틀은 제목과 문장이 같은 출발·도착 지명이다(사용자 결정 2026-10-30). 틀 문장은 Timeline 입력에 실린다.
    assert bases[1].description == "관악구 대학동에서 마포구 도화동으로 이동했다."
    assert bases[1].as_prompt_dict()["description"] == bases[1].description
    assert all("title" not in base.as_prompt_dict() for base in bases), "제목은 Timeline 이 쓴다."


# --- 일정을 기본 event 에 넣는다 ---------------------------------------------------------------


def _meeting_day(meeting_start, meeting_end):
    from tests.fixtures.requests import calendar_item

    office = stay_item("office", lat=37.55, start="2026-06-20T12:08:00", end="2026-06-20T20:36:00", address="서울 마포구 도화동 17-13")
    station = stay_item("station", lat=37.70, start="2026-06-20T22:34:00", end="2026-06-20T22:59:00", address="서울 용산구 청파로 378")
    meeting = calendar_item("meeting", "팀 정기 회의", start=meeting_start, end=meeting_end)
    return make_request(stays=[office, station], calendars=[meeting])


def _clues_with_calendars(request):
    from app.services.calendar_clues import build_calendar_clues

    return build_timeline_clues(build_location_skeleton(request), build_calendar_clues(request), [], [])


def test_a_calendar_covering_a_stay_becomes_that_stays_base_event():
    """일정이 덮은 체류의 기본 event 는 그 일정의 것이다(사용자 결정 2026-10-30).

    "캘린더의 경우 Timeline이 처리하기 전에 Location 의 결과물에다가 박아넣어버려". 회의 일정(12:00~23:00)과
    같은 체류(12:08~20:36)를 Timeline 이 회의와 `보낸 시간` 으로 따로 썼다. 일정이 주인이고 체류는 그 위치
    근거다. 같은 일정 시간 안의 다른 장소 체류(22:34 청파로)는 자기 기본 event 그대로다(위치 뼈대는 살린다).
    """

    from app.schemas import EventType

    request = _meeting_day("2026-06-20T12:00:00", "2026-06-20T23:00:00")
    clues = _clues_with_calendars(request)
    office, station = [b.base_event for b in clues.bundles if b.base_event]

    assert office.event_type is EventType.CALENDAR_EVENT
    assert office.title == "팀 정기 회의 일정이 있었음", "제목에 지명을 붙이지 않고 음슴체로 끝난다(#179)."
    assert office.description == "{place}에서 팀 정기 회의 일정이 있었다."
    assert (office.start.hour, office.end.hour) == (12, 23), "시간은 일정 시간이다."
    prompt = office.as_prompt_dict()
    assert prompt["calendar"] == office.calendar.clue_id and prompt["startAt"].startswith("2026-06-20T12:00")
    assert station.calendar is None and station.event_type is EventType.STAY


def test_a_short_calendar_inside_a_long_stay_stays_a_clue():
    """9시간 체류 안의 1시간 회의는 체류의 일부라 기본 event 를 바꾸지 않는다 — Timeline 이 체류 안으로 꺼낸다."""

    request = _meeting_day("2026-06-20T14:00:00", "2026-06-20T15:00:00")
    clues = _clues_with_calendars(request)

    assert all(b.base_event.calendar is None for b in clues.bundles if b.base_event)
    assert any(b.calendars for b in clues.bundles), "일정은 그 체류 묶음의 단서로 남는다."


def test_restore_counts_the_calendar_event_and_brings_back_the_rest_as_a_stay():
    """일정이 빠져 캘린더 복원이 되살린 일정 event 도 그 체류를 덮는다. 일정 event 가 체류 일부만 덮으면
    나머지는 일정이 아니라 체류 틀로 되살린다."""

    from datetime import datetime, timedelta, timezone

    from app.schemas import EventSourceType, EventType, InferenceLevel, SourceRef, TimelineDraft, TimelineEventDraft
    from app.services.skeleton_guard import ensure_skeleton_segments

    kst = timezone(timedelta(hours=9))
    request = _meeting_day("2026-06-20T12:00:00", "2026-06-20T23:00:00")
    clues = _clues_with_calendars(request)
    office = next(b.base_event for b in clues.bundles if b.base_event and b.base_event.calendar)

    def meeting(end_hour):
        return TimelineEventDraft(
            client_event_id="calendar-restored-001", event_type=EventType.CALENDAR_EVENT, title="팀 정기 회의",
            description="캘린더에 적어 둔 일정이다.", start_time=datetime(2026, 6, 20, 12, tzinfo=kst),
            end_time=datetime(2026, 6, 20, end_hour, tzinfo=kst), confidence=0.5, inference_level=InferenceLevel.DIRECT,
            source_refs=[SourceRef(source_type=EventSourceType.CALENDAR, raw_id=office.calendar.raw_id)],
        )

    whole = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[meeting(23)])
    ensure_skeleton_segments(whole, request, clues)
    assert not [e for e in whole.events if office.base_id in e.base_event_ids], "일정 event 가 덮었다."

    part = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[meeting(15)])
    ensure_skeleton_segments(part, request, clues)
    [rest] = [e for e in part.events if office.base_id in e.base_event_ids]
    assert rest.event_type is EventType.STAY and rest.description == "{place}에서 시간을 보냈다."
    assert rest.start_time.hour == 15


@pytest.mark.parametrize("cites_calendar", [True, False])
def test_the_calendar_base_event_ends_up_as_one_event_at_the_calendar_time(cites_calendar):
    """Timeline 이 일정과 같은 체류의 `보낸 시간` 을 따로 쓰거나, 일정 event 에 일정 근거를 빠뜨린 실제 출력.

    일정 기본 event 에서 온 event 중 체류를 덮는 것은 하나로 합치고 일정이 주인이다. 시간은 일정 시간이고,
    체류 일부만 덮는 식사는 그대로다.
    """

    from datetime import datetime, timedelta, timezone

    from app.schemas import EventSourceType, EventType, InferenceLevel, SourceRef, TimelineDraft, TimelineEventDraft
    from app.services.skeleton_guard import keep_calendar_base_events

    kst = timezone(timedelta(hours=9))
    request = _meeting_day("2026-06-20T12:00:00", "2026-06-20T23:00:00")
    clues = _clues_with_calendars(request)
    office = next(b.base_event for b in clues.bundles if b.base_event and b.base_event.calendar)
    stay_refs = [SourceRef(source_type=EventSourceType.STAY, raw_id=r) for r in office.segment.raw_ids]

    def event(title, event_type, start, end, refs):
        return TimelineEventDraft(
            client_event_id=f"event-{title}", event_type=event_type, title=title, description="",
            start_time=datetime(2026, 6, 20, *start, tzinfo=kst), end_time=datetime(2026, 6, 20, *end, tzinfo=kst),
            confidence=0.7, inference_level=InferenceLevel.DIRECT, source_refs=list(refs),
            base_event_ids=[office.base_id],
        )

    calendar_ref = [SourceRef(source_type=EventSourceType.CALENDAR, raw_id=office.calendar.raw_id)]
    meeting = event("회의", EventType.MEETING, (12, 8), (20, 36), stay_refs + (calendar_ref if cites_calendar else []))
    stayed = event("보낸 시간", EventType.STAY, (12, 8), (20, 36), stay_refs)
    meal = event("식사", EventType.MEAL, (17, 24), (18, 4), stay_refs)
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[meeting, stayed, meal])

    keep_calendar_base_events(draft, request, clues)

    assert [e.title for e in draft.events] == ["회의", "식사"]
    assert any(ref.raw_id == office.calendar.raw_id for ref in meeting.source_refs), "일정이 주인이다."
    assert (meeting.start_time.hour, meeting.end_time.hour) == (12, 23), "시간은 일정 시간이다."


def test_photos_in_a_location_bundle_carry_no_place_of_their_own():
    """같은 곳에 두 번 간 날 사진의 주소·장소 후보가 아침 체류와 글자까지 같아, LLM 이 그 이름으로 12:49 사진을
    아침 체류에 이었다. 위치 구간에 붙은 사진은 묶음이 장소를 말하므로 사진의 장소를 싣지 않는다(사용자 결정
    2026-10-31). 구간에 붙지 않은 사진은 자기 장소가 필요해 그대로 싣는다."""

    import json
    from datetime import datetime, timedelta, timezone

    from app.schemas.clues import PhotoClue

    kst = timezone(timedelta(hours=9))
    office = stay_item("office", lat=37.55, start="2026-06-20T09:00:00", end="2026-06-20T18:00:00", address="서울 마포구 도화동 17-13")
    request = make_request(stays=[office])
    inside = PhotoClue(clue_id="P1", raw_id=fixture_raw_id("photo-in"), taken_at=datetime(2026, 6, 20, 12, 30, tzinfo=kst),
                       places=["OO식당"], address="서울 마포구 도화동 17-13", latitude=37.55, longitude=127.0)
    outside = PhotoClue(clue_id="P2", raw_id=fixture_raw_id("photo-out"), taken_at=datetime(2026, 6, 20, 22, 0, tzinfo=kst),
                        places=["OO포차"], address="서울 마포구 연남동 1", latitude=37.56, longitude=126.92)
    clues = build_timeline_clues(build_location_skeleton(request), [], [inside, outside], [])

    sections = json.loads(json.dumps(timeline_input_sections(clues), ensure_ascii=False, default=str))
    text = json.dumps(sections, ensure_ascii=False)
    photo_payloads = {p["id"]: p for p in _find_photos(sections)}

    assert "address" not in photo_payloads["P1"] and "places" not in photo_payloads["P1"]
    assert photo_payloads["P2"]["places"] == ["OO포차"], "구간에 붙지 않은 사진은 자기 장소를 싣는다."
    assert "OO식당" in text, "장소 후보는 묶음의 places 에 남는다."


def _find_photos(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "photos" and isinstance(value, list):
                yield from value
            else:
                yield from _find_photos(value)
    elif isinstance(node, list):
        for item in node:
            yield from _find_photos(item)
    elif isinstance(node, str) and node.lstrip().startswith(("{", "[")):
        import json

        try:
            yield from _find_photos(json.loads(node))
        except ValueError:
            return


def test_a_meal_holding_the_calendar_becomes_the_calendar_event_and_goes_to_repair():
    """Repair 가 회의 event 를 식사로 바꾸며 묶음 id 로 일정을 다시 담은 실제 출력. 일정을 가진 식사가 일정의
    주인으로 남자 식사 시간이 20분으로 줄여 회의 일정이 17:24~17:44 가 됐다. 일정 기본 event 의 주인은 일정
    event 다 — 식사·사진·`보낸 시간` 이면 종류와 시간을 일정으로 맞추고 글은 Repair 에 맡긴다.
    """

    from datetime import datetime, timedelta, timezone

    from app.schemas import EventSourceType, EventType, InferenceLevel, SourceRef, TimelineDraft, TimelineEventDraft
    from app.services.skeleton_guard import NEEDS_WRITING, keep_calendar_base_events

    kst = timezone(timedelta(hours=9))
    request = _meeting_day("2026-06-20T12:00:00", "2026-06-20T23:00:00")
    clues = _clues_with_calendars(request)
    office = next(b.base_event for b in clues.bundles if b.base_event and b.base_event.calendar)
    meal = TimelineEventDraft(
        client_event_id="event-005", event_type=EventType.MEAL, title="점심", description="{place}에서 점심을 먹고 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 12, tzinfo=kst), end_time=datetime(2026, 6, 20, 23, tzinfo=kst),
        confidence=0.7, inference_level=InferenceLevel.INFERRED, base_event_ids=[office.base_id],
        source_refs=[
            *(SourceRef(source_type=EventSourceType.STAY, raw_id=r) for r in office.segment.raw_ids),
            SourceRef(source_type=EventSourceType.CALENDAR, raw_id=office.calendar.raw_id),
        ],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[meal])

    keep_calendar_base_events(draft, request, clues)

    assert meal.event_type is EventType.CALENDAR_EVENT
    assert meal.merge_reason == NEEDS_WRITING
    assert meal.title == "점심", "글은 코드가 바꾸지 않는다."
    assert (meal.start_time.hour, meal.end_time.hour) == (12, 23)


def _kst_event(title, event_type, start, end, refs, base=()):
    from datetime import datetime, timedelta, timezone

    from app.schemas import InferenceLevel, TimelineEventDraft

    kst = timezone(timedelta(hours=9))
    return TimelineEventDraft(
        client_event_id=f"event-{title}", event_type=event_type, title=title, description="",
        start_time=datetime(2026, 6, 20, *start, tzinfo=kst), end_time=datetime(2026, 6, 20, *end, tzinfo=kst),
        confidence=0.7, inference_level=InferenceLevel.DIRECT, source_refs=list(refs), base_event_ids=list(base),
    )


def test_the_calendar_evidence_stays_on_its_owner_only():
    """일정 시간과 조금 겹친 이동·다른 장소 체류가 같은 일정을 근거로 가진 실제 출력. 일정 근거는 주인 하나다."""

    from app.schemas import EventSourceType, EventType, SourceRef, TimelineDraft
    from app.services.skeleton_guard import keep_calendar_base_events

    request = _meeting_day("2026-06-20T12:00:00", "2026-06-20T23:00:00")
    clues = _clues_with_calendars(request)
    office = next(b.base_event for b in clues.bundles if b.base_event and b.base_event.calendar)
    calendar_ref = SourceRef(source_type=EventSourceType.CALENDAR, raw_id=office.calendar.raw_id)
    stay_ref = SourceRef(source_type=EventSourceType.STAY, raw_id=request.stays[1].raw_id)
    meeting = _kst_event("회의", EventType.MEETING, (12, 0), (23, 0), [calendar_ref], base=[office.base_id])
    station = _kst_event("청파로", EventType.STAY, (22, 34), (22, 59), [stay_ref, calendar_ref])
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[meeting, station])

    keep_calendar_base_events(draft, request, clues)

    assert [r.raw_id for r in station.source_refs] == [request.stays[1].raw_id]
    assert office.calendar.raw_id in [r.raw_id for r in meeting.source_refs]


def test_a_meal_appointment_stays_a_meal():
    """식사 약속은 일정이 식사다. 3시간 이하면 식사가 일정의 주인으로 남는다 — 일정 틀로 되돌리면 무엇을 먹었는지가 사라진다."""

    from app.schemas import EventSourceType, EventType, SourceRef, TimelineDraft
    from app.services.skeleton_guard import keep_calendar_base_events
    from tests.fixtures.requests import calendar_item

    lunch_place = stay_item("lunch", lat=37.55, start="2026-06-20T12:05:00", end="2026-06-20T12:55:00", address="서울 마포구 도화동 17-13")
    appointment = calendar_item("lunch", "점심 약속", start="2026-06-20T12:00:00", end="2026-06-20T13:00:00")
    request = make_request(stays=[lunch_place], calendars=[appointment])
    clues = _clues_with_calendars(request)
    base = next(b.base_event for b in clues.bundles if b.base_event and b.base_event.calendar)
    meal = _kst_event(
        "순두부찌개", EventType.MEAL, (12, 0), (13, 0),
        [SourceRef(source_type=EventSourceType.CALENDAR, raw_id=appointment.raw_id)], base=[base.base_id],
    )

    keep_calendar_base_events(
        TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[meal]), request, clues
    )

    assert meal.event_type is EventType.MEAL and meal.title == "순두부찌개"



@pytest.mark.parametrize(
    "address, expected",
    [
        ("대구 북구 산격동 1346", "북구 산격동"),
        ("서울특별시 마포구 백범로31길 7", "마포구 백범로31길"),
        ("대전광역시 동구 옥천로180번길 47-2", "동구 옥천로180번길"),
        ("경기도 오산시 경기대로 645", "오산시 경기대로"),
        ("경기 안산시 상록구 팔곡이동 산 3-11", "상록구 팔곡이동"),
        ("충북 청주시 흥덕구 오송읍 봉산리 804", "흥덕구 오송읍"),
        ("경북 칠곡군 지천면 덕산리 406-9", "칠곡군 지천면"),
        ("경기도 성남시 분당구 정자동 178-1", "분당구 정자동"),
        ("세종특별자치시 한솔동 123", "한솔동"),
        ("전남광주통합특별시 여수시 여서동 123", "여수시 여서동"),
        ("서울특별시 노원구 상계3.4동 123", "노원구 상계3.4동"),
        ("서울특별시 종로구 종로1.2.3.4가동", "종로구 종로1.2.3.4가동"),
        ("서울특별시 중구 을지로1가 16", "중구 을지로1가"),
        ("대구 수성구 범어동 123 우성동", "수성구 범어동"),
        ("대구광역시 수성구 지범로17길 85 101동", "수성구 지범로17길"),
        ("서울특별시 강남구 테헤란로 152 (역삼동)", "강남구 테헤란로"),
        ("서울특별시 강남구 테헤란로 152 강남파이낸스동", "강남구 테헤란로"),
        ("서울특별시", None),
        ("1-2-3 Shibuya, Tokyo", None),
    ],
)
def test_the_area_is_the_district_and_the_neighborhood_without_the_number(address, expected):
    """지명은 구 + 동이다. 번지·건물명을 읽지 않고, 읍·면이 있으면 리가 아니라 읍·면이다."""

    from app.services.base_events import AreaNamer

    assert AreaNamer().name(address) == expected
