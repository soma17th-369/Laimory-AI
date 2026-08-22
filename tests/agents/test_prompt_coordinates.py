"""프롬프트에 좌표를 싣지 않는다 (#80).

위경도는 사람이 읽고 판단할 값이 아니다. Agent 가 직접 해석할 일이 없는데도 원본 항목마다
실려 나가면서 input token 만 차지한다. 좌표가 필요한 판단(연속 MOVEMENT 사이 끝점 거리 등)은
코드가 `derivedMetrics` 로 계산해 결론만 넘기므로 원본에서 빼도 근거가 줄지 않는다.

**입력 스키마에서 없앤 것이 아니다.** request 로는 그대로 받고 코드가 계속 쓴다.
"""

import json

from app.agents.events.location.agent import _location_data_text
from app.agents.events.photo.agent import _photo_items_to_text
from app.agents.repair.tools import RepairContext, _lookup_source
from app.schemas import GeoPlace, TimelineDraft
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    movement_item,
    photo_item,
    stay_item,
)

COORDINATE_KEYS = ("latitude", "longitude", '"lat"', '"lon"')


def _request():
    movement = movement_item(2, raw_id="move-1")
    movement.start = GeoPlace(latitude=37.15, longitude=127.07, place="집", places=["집"])
    movement.end = GeoPlace(
        latitude=37.53, longitude=126.96, place="한강공원", places=["한강공원"]
    )
    return make_request(
        stays=[stay_item(1, raw_id="stay-1", lat=37.15, lon=127.07, place="집")],
        movements=[movement],
        photos=[photo_item(3, raw_id="photo-1", lat=37.15, lon=127.07)],
    )


def _assert_no_coordinates(text: str) -> None:
    for key in COORDINATE_KEYS:
        assert key not in text, f"{key} 가 프롬프트에 남아 있습니다"


def test_location_prompt_has_no_coordinates():
    request = _request()

    text = _location_data_text(request, [*request.stays, *request.movements])

    _assert_no_coordinates(text)


def test_location_prompt_keeps_every_other_field():
    request = _request()

    payload = json.loads(_location_data_text(request, [*request.stays, *request.movements]))

    stay, movement = payload["locationItems"]
    assert stay["place"] == "집"
    assert movement["end"]["place"] == "한강공원"
    assert movement["end"]["places"] == ["한강공원"]
    # 파생 지표는 그대로다 — 좌표로 계산한 값도 남는다.
    assert "derivedMetrics" in payload


def test_photo_prompt_has_no_coordinates():
    request = _request()

    _assert_no_coordinates(_photo_items_to_text(request.photos))


def test_photo_prompt_keeps_place_fields():
    photo = photo_item(9, raw_id="photo-9", lat=37.1, lon=127.0, places=["한강공원"], address="서울 영등포구 여의동로 330")

    payload = json.loads(_photo_items_to_text([photo]))

    assert payload[0]["places"] == ["한강공원"]
    assert payload[0]["address"] == "서울 영등포구 여의동로 330"


def test_lookup_source_has_no_coordinates():
    request = _request()
    ctx = RepairContext(
        request=request,
        draft=TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul"),
    )

    text = _lookup_source(ctx, {"rawId": fixture_raw_id("stay-1")})

    _assert_no_coordinates(text)
    assert "집" in text  # 좌표만 빠지고 나머지는 그대로


def test_empty_input_is_unchanged():
    assert _photo_items_to_text([]) == "없음"
