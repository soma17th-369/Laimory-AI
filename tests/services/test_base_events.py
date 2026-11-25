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
    시간은 체류다 — 방문의 경계는 위치 기록이 정한다(#202).
    """

    from app.schemas import EventType

    request = _meeting_day("2026-06-20T12:00:00", "2026-06-20T23:00:00")
    clues = _clues_with_calendars(request)
    office, station = [b.base_event for b in clues.bundles if b.base_event]

    assert office.event_type is EventType.CALENDAR_EVENT
    assert office.title == "팀 정기 회의 일정이 있었음", "제목에 지명을 붙이지 않고 음슴체로 끝난다(#179)."
    assert office.description == "{place}에서 팀 정기 회의 일정이 있었다."
    assert (office.start, office.end) == (office.segment.start, office.segment.end), "시간은 체류 구간이다."
    assert (office.start.hour, office.start.minute, office.end.hour, office.end.minute) == (12, 8, 20, 36)
    assert station.calendar is None and station.event_type is EventType.STAY


def test_a_short_calendar_inside_a_long_stay_is_its_own_event_inside_the_visit():
    """9시간 체류 안의 1시간 회의는 방문과 회의 두 건이다(#202). 방문의 경계는 그대로다."""

    request = _meeting_day("2026-06-20T14:00:00", "2026-06-20T15:00:00")
    clues = _clues_with_calendars(request)
    office, meeting, station = clues.base_events

    assert office.calendar is None and (office.start.hour, office.end.hour) == (12, 20)
    assert meeting.segment is None and (meeting.start.hour, meeting.end.hour) == (14, 15)
    assert meeting.inside == office.base_id
    assert [r.raw_id for r in meeting.place_records] == office.segment.raw_ids, "방문의 체류가 장소 후보의 근거다."
    assert station.calendar is None



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



def _kst_event(title, event_type, start, end, refs, base=()):
    from datetime import datetime, timedelta, timezone

    from app.schemas import InferenceLevel, TimelineEventDraft

    kst = timezone(timedelta(hours=9))
    return TimelineEventDraft(
        client_event_id=f"event-{title}", event_type=event_type, title=title, description="",
        start_time=datetime(2026, 6, 20, *start, tzinfo=kst), end_time=datetime(2026, 6, 20, *end, tzinfo=kst),
        confidence=0.7, inference_level=InferenceLevel.DIRECT, source_refs=list(refs), base_event_ids=list(base),
    )



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
