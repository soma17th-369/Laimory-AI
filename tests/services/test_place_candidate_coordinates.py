"""입력 조회 v2 와 장소 후보 좌표 (App Server 명세 2026-10-30, 사용자 결정 2026-10-31).

입력은 `places` 를 이름+좌표 객체로 받고, 결과는 후보마다 그 원소의 좌표를 싣는다. 후보 이름과 좌표는
저장 직전에 그 event 의 입력과 대조한다. 좌표 하나가 틀려도 App Server 는 결과 batch 전체를 400 으로
거절하므로, 틀린 값이 나갈 길이 없어야 한다.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agents.parsing import items_to_text_without_prompt_excluded_keys
from app.schemas import (
    CalendarItem,
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    MovementItem,
    SourceRef,
    StayItem,
    TimelineDraft,
    TimelineEventDraft,
)
from app.schemas.common import PlacePoint
from app.schemas.photo import PhotoItem
from app.schemas.timeline_result import PlaceCandidate, TimelineResultEvent, TimelineResultRequest
from app.services.clue_bundles import build_timeline_clues, timeline_input_sections
from app.services.location_skeleton import build_location_skeleton
from app.services.timeline_result import build_result_request
from app.services.timeline_validator import PlaceCandidateViolation, checked_place_candidates
from tests.fixtures.requests import calendar_item, fixture_raw_id, make_request, photo_item

_KST = timezone(timedelta(hours=9))
_RAW = fixture_raw_id("stay-1")


def _objects(*names):
    return [
        {"name": name, "latitude": 37.5 + index / 1000, "longitude": 127.0 + index / 1000}
        for index, name in enumerate(names)
    ]


def _stay(label, places, *, start="2026-06-20T09:00:00+09:00", end="2026-06-20T18:00:00+09:00",
          lat=37.5, lon=127.0, address="서울 마포구 도화동 17-13"):
    return StayItem.model_validate(
        {
            "rawId": fixture_raw_id(f"stay-{label}"), "startAt": start, "endAt": end,
            "latitude": lat, "longitude": lon, "address": address, "places": places,
        }
    )


def _event(refs, **overrides):
    values = dict(
        client_event_id="event-001",
        event_type=EventType.UNKNOWN,
        title="마포구 도화동에서 보낸 시간",
        description="{place}에서 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 9, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 18, 0, tzinfo=_KST),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=kind, raw_id=raw_id) for kind, raw_id in refs],
    )
    values.update(overrides)
    return TimelineEventDraft(**values)


def _draft(*events):
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


def _body(draft, request):
    return build_result_request(draft, request).model_dump(by_alias=True, mode="json")


# --- 입력 조회 v2 ---------------------------------------------------------------------------


def test_v2_places_keep_the_names_and_carry_the_coordinates_apart():
    """`places` 는 이름 목록 그대로이고 좌표는 새 필드에 있다 — 스키마는 추가만 한다."""

    places = [
        {"name": "OO빌딩", "latitude": 37.5443, "longitude": 127.0557},
        {"name": "스타벅스 성수점", "latitude": 37.54418, "longitude": 127.05592},
    ]

    stay = _stay("a", places)
    photo = PhotoItem.model_validate(
        {"rawId": fixture_raw_id("photo"), "takenAt": "2026-06-20T12:00:00+09:00", "places": places}
    )
    spot = GeoPlace.model_validate({"latitude": 37.5, "longitude": 127.0, "places": places})

    for item in (stay, photo, spot):
        assert item.places == ["OO빌딩", "스타벅스 성수점"]
        assert item.place_points[1] == PlacePoint(name="스타벅스 성수점", latitude=37.54418, longitude=127.05592)


def test_v1_places_stay_as_they_were():
    stay = _stay("a", ["스타벅스 성수점"])

    assert stay.places == ["스타벅스 성수점"]
    assert stay.place_points == []


@pytest.mark.parametrize(
    "broken",
    [
        {"name": "뒤바뀐 좌표", "latitude": 127.05, "longitude": 37.54},
        {"name": "한쪽만", "latitude": 37.54},
        {"name": "NaN", "latitude": float("nan"), "longitude": 127.0},
        {"name": "문자열 좌표", "latitude": "37.54", "longitude": None},
    ],
)
def test_a_broken_coordinate_keeps_the_name_and_loses_only_the_point(broken):
    """좌표 하나 때문에 입력 항목을 버리면 하루치가 실패한다. 이름은 후보로 쓸 수 있다."""

    stay = _stay("a", [broken, "문자열로 온 이름", {"name": "멀쩡한 곳", "latitude": 37.5, "longitude": 127.0}])

    assert stay.places == [broken["name"], "문자열로 온 이름", "멀쩡한 곳"]
    assert [point.name for point in stay.place_points] == ["멀쩡한 곳"]


def test_prompts_are_the_same_for_v1_and_v2_input():
    """좌표는 프롬프트에 싣지 않는다(#80). 입력 버전이 바뀌어도 LLM 이 보는 글은 같다."""

    names = ["OO빌딩", "스타벅스 성수점"]
    v1, v2 = _stay("a", names), _stay("a", _objects(*names))

    assert items_to_text_without_prompt_excluded_keys([v1]) == items_to_text_without_prompt_excluded_keys([v2])

    def sections(stay):
        request = make_request(stays=[stay])
        return timeline_input_sections(build_timeline_clues(build_location_skeleton(request), [], [], []))

    assert sections(v1) == sections(v2)


# --- 결과의 후보 좌표 ------------------------------------------------------------------------


def test_each_candidate_carries_the_coordinates_of_its_own_input_element():
    places = [
        {"name": "OO빌딩", "latitude": 37.5443, "longitude": 127.0557},
        {"name": "스타벅스 성수점", "latitude": 37.54418, "longitude": 127.05592},
    ]
    stay = _stay("a", places)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)],
        place="스타벅스 성수점", place_candidates=["스타벅스 성수점", "OO빌딩"],
    )

    [body] = _body(_draft(event), make_request(stays=[stay]))["events"]

    assert body["placeCandidates"] == [
        {"name": "스타벅스 성수점", "latitude": 37.54418, "longitude": 127.05592},
        {"name": "OO빌딩", "latitude": 37.5443, "longitude": 127.0557},
    ]
    assert body["place"] == "스타벅스 성수점"


def test_a_candidate_outside_the_input_places_has_no_coordinate_keys():
    """일정 장소명처럼 `places` 밖에서 만든 후보는 좌표 없이 나간다(명세 예시의 `모수 성수`)."""

    stay = _stay("a", _objects("스타벅스 성수점"))
    meeting = calendar_item(
        "m", "약속", start="2026-06-20T09:00:00+09:00", end="2026-06-20T18:00:00+09:00", location_text="모수 성수",
    )
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.CALENDAR, meeting.raw_id)],
        place="모수 성수", place_candidates=["모수 성수", "스타벅스 성수점"],
    )

    [body] = _body(_draft(event), make_request(stays=[stay], calendars=[meeting]))["events"]

    assert body["placeCandidates"][0] == {"name": "모수 성수"}
    assert set(body["placeCandidates"][1]) == {"name", "latitude", "longitude"}


def test_v1_input_sends_names_only():
    stay = _stay("a", ["스타벅스 성수점"])
    event = _event([(EventSourceType.STAY, stay.raw_id)], place_candidates=["스타벅스 성수점"])

    [body] = _body(_draft(event), make_request(stays=[stay]))["events"]

    assert body["placeCandidates"] == [{"name": "스타벅스 성수점"}]


def test_the_same_name_elsewhere_does_not_lend_its_coordinates():
    """체인점처럼 이름이 같아도 다른 곳(1km 밖)의 원소 좌표는 쓰지 않는다. 그 event 의 입력이 먼저다."""

    here = _stay("here", [{"name": "스타벅스", "latitude": 37.50, "longitude": 127.00}])
    there = _stay(
        "there", [{"name": "스타벅스", "latitude": 37.60, "longitude": 127.10}],
        start="2026-06-20T19:00:00+09:00", end="2026-06-20T20:00:00+09:00",
        lat=37.60, lon=127.10, address="서울 강남구 강남대로 396",
    )
    event = _event([(EventSourceType.STAY, here.raw_id)], place_candidates=["스타벅스"])

    [body] = _body(_draft(event), make_request(stays=[here, there]))["events"]

    assert body["placeCandidates"] == [{"name": "스타벅스", "latitude": 37.50, "longitude": 127.00}]


def test_a_name_from_the_same_place_keeps_its_own_coordinates():
    """같은 장소(250m)의 다른 기록에 있는 이름도 후보가 될 수 있고, 좌표는 그 기록의 원소 값이다."""

    morning = _stay("am", _objects("OO빌딩"), end="2026-06-20T12:00:00+09:00")
    afternoon = _stay(
        "pm", [{"name": "모수 성수", "latitude": 37.5447, "longitude": 127.0561}],
        start="2026-06-20T13:00:00+09:00", address="서울 마포구 도화동 17-14", lat=37.5001,
    )
    event = _event(
        [(EventSourceType.STAY, morning.raw_id)],
        end_time=datetime(2026, 6, 20, 12, 0, tzinfo=_KST), place_candidates=["OO빌딩", "모수 성수"],
    )

    [body] = _body(_draft(event), make_request(stays=[morning, afternoon]))["events"]

    assert body["placeCandidates"][1] == {"name": "모수 성수", "latitude": 37.5447, "longitude": 127.0561}


# --- 후보 정합성 검사 ------------------------------------------------------------------------


def test_candidates_are_checked_against_the_events_input():
    point = PlacePoint(name="스타벅스", latitude=37.5, longitude=127.0)
    sources = {"스타벅스": point, "모수성수": None}

    kept, dropped = checked_place_candidates(
        ["스타벅스", " 스타벅스 ", "", "지어낸 식당", "모수 성수"], sources
    )

    assert [c.name for c in kept] == ["스타벅스", "모수 성수"]
    assert (kept[0].latitude, kept[1].latitude) == (37.5, None)
    assert dropped == {
        PlaceCandidateViolation.DUPLICATE_NAME: 1,
        PlaceCandidateViolation.BLANK_NAME: 1,
        PlaceCandidateViolation.NAME_NOT_IN_EVIDENCE: 1,
    }


def test_no_more_than_five_candidates_go_out():
    names = [f"장소{index}" for index in range(7)]
    stay = _stay("a", _objects(*names))
    event = _event([(EventSourceType.STAY, stay.raw_id)], place_candidates=names)

    [body] = _body(_draft(event), make_request(stays=[stay]))["events"]

    assert [c["name"] for c in body["placeCandidates"]] == names[:5]


def test_a_name_from_another_place_is_dropped_and_place_is_the_first_left():
    """다른 장소의 이름은 그 event 의 후보가 아니다. `place` 는 언제나 남은 1번 후보다."""

    here = _stay("here", _objects("OO빌딩"))
    there = _stay(
        "there", _objects("멀리 있는 식당"), start="2026-06-20T19:00:00+09:00",
        end="2026-06-20T20:00:00+09:00", lat=37.60, lon=127.10, address="서울 강남구 강남대로 396",
    )
    event = _event(
        [(EventSourceType.STAY, here.raw_id)],
        place="멀리 있는 식당", place_candidates=["멀리 있는 식당", "OO빌딩"],
    )

    [body] = _body(_draft(event), make_request(stays=[here, there]))["events"]

    assert [c["name"] for c in body["placeCandidates"]] == ["OO빌딩"]
    assert body["place"] == "OO빌딩"


def test_when_every_candidate_is_dropped_the_slot_goes_to_the_area():
    """칸이 남은 채 장소가 없으면 App Server 가 하루치를 400 으로 거절한다."""

    stay = _stay("a", _objects("OO빌딩"))
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)],
        place="지어낸 식당", place_candidates=["지어낸 식당"], address="서울 마포구 도화동 17-13",
    )

    [body] = _body(_draft(event), make_request(stays=[stay]))["events"]

    assert "placeCandidates" not in body
    assert body["place"] is None
    assert body["subtitle"] == "마포구 도화동에서 시간을 보냈다.", "주소를 번지까지 통째로 넣지 않는다."


def test_events_without_candidates_keep_their_place():
    """v3 는 후보를 쓰지 않는다. `place` 와 body 가 그대로다."""

    stay = _stay("a", _objects("OO빌딩"))
    event = _event([(EventSourceType.STAY, stay.raw_id)], place="회사", description="회사에서 일했다.")

    [body] = _body(_draft(event), make_request(stays=[stay]))["events"]

    assert body["place"] == "회사"
    assert "placeCandidates" not in body


# --- 결과 JSON 계약 -------------------------------------------------------------------------


def test_the_result_body_is_strict_json_and_reads_back_the_same():
    """명세의 모양 그대로 나가고, NaN 없는 JSON 이고, 같은 계약으로 다시 읽힌다."""

    stay = _stay("a", _objects("스타벅스 성수점", "모수 성수"))
    event = _event(
        [(EventSourceType.STAY, stay.raw_id)], place_candidates=["스타벅스 성수점", "모수 성수"],
    )

    body = _body(_draft(event), make_request(stays=[stay]))
    text = json.dumps(body, allow_nan=False, ensure_ascii=False)

    assert TimelineResultRequest.model_validate(json.loads(text)).model_dump(by_alias=True, mode="json") == body
    [event_body] = body["events"]
    assert {"eventType", "title", "subtitle", "startAt", "endAt", "sourceRawIds"} <= set(event_body)
    assert event_body["sourceRawIds"] == [stay.raw_id]


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "한쪽만", "latitude": 37.5},
        {"name": "뒤바뀜", "latitude": 127.0, "longitude": 37.5},
        {"name": "경도 범위", "latitude": 37.5, "longitude": 181},
        {"name": "NaN", "latitude": float("nan"), "longitude": 127.0},
        {"name": "무한대", "latitude": 37.5, "longitude": float("inf")},
        {"name": ""},
        {"name": "가" * 256},
    ],
)
def test_the_contract_refuses_what_app_server_would_reject(bad):
    with pytest.raises(ValidationError):
        PlaceCandidate(**bad)


def test_the_range_limits_are_allowed():
    PlaceCandidate(name="경계", latitude=-90, longitude=180)


def test_the_contract_refuses_more_than_five_candidates():
    with pytest.raises(ValidationError):
        TimelineResultEvent(
            event_type=EventType.UNKNOWN, title="제목", start_at=datetime(2026, 6, 20, 9, tzinfo=_KST),
            end_at=datetime(2026, 6, 20, 10, tzinfo=_KST), source_raw_ids=[_RAW],
            place_candidates=[PlaceCandidate(name=f"장소{index}") for index in range(6)],
        )


def test_movement_end_places_feed_the_same_shape():
    movement = MovementItem.model_validate(
        {
            "rawId": fixture_raw_id("movement"), "startAt": "2026-06-20T08:00:00+09:00",
            "endAt": "2026-06-20T09:00:00+09:00",
            "start": {"places": _objects("출발지")}, "end": {"places": _objects("도착지")},
        }
    )

    assert movement.start.places == ["출발지"]
    assert movement.end.place_points[0].name == "도착지"


def test_calendar_items_are_untouched_by_the_v2_input():
    assert "places" not in CalendarItem.model_fields
    assert photo_item("p").places == []
