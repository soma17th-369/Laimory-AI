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

    assert [base.title for base in bases] == ["관악구 대학동에서 보낸 시간", "이동", "마포구 도화동에서 보낸 시간"]
    assert all("title" not in base.as_prompt_dict() for base in bases), "제목은 Timeline 이 쓴다."
