"""TimelineDraft → 결과 저장 요청 변환 검증."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import EventSourceType, EventType, InferenceLevel, SourceRef
from app.schemas.timeline import TimelineDraft, TimelineEventDraft
from app.schemas import GeoPlace, MovementItem, TimelineResultEvent
from app.services.timeline_result import build_result_request
from tests.fixtures.requests import fixture_raw_id, make_request, photo_item, stay_item

_KST = timezone(timedelta(hours=9))


def _event(**overrides) -> TimelineEventDraft:
    defaults = dict(
        client_event_id="evt-1",
        event_type=EventType.MEAL,
        title="점심",
        description="근처 식당에서 식사",
        start_time=datetime(2026, 7, 22, 12, 0, tzinfo=_KST),
        end_time=datetime(2026, 7, 22, 13, 0, tzinfo=_KST),
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id("result-1"),
            )
        ],
    )
    defaults.update(overrides)
    return TimelineEventDraft(**defaults)


def _draft(*events: TimelineEventDraft) -> TimelineDraft:
    return TimelineDraft(
        user_id="u-1",
        date="2026-07-22",
        timezone="Asia/Seoul",
        events=list(events),
    )


def _build(draft: TimelineDraft, request=None):
    """입력이 필요 없는 검증은 빈 입력으로 만든다. 좌표는 입력이 없으면 비어 있다."""

    return build_result_request(draft, request or make_request())


def test_maps_draft_event_to_contract_fields():
    request = _build(_draft(_event()))

    [event] = request.events
    assert event.event_type is EventType.MEAL
    assert event.title == "점심"
    assert event.subtitle == "근처 식당에서 식사"
    assert event.source_raw_ids == [fixture_raw_id("result-1")]


def test_carries_the_record_question_to_the_contract():
    request = _build(
        _draft(_event(question="점심 자리에서 어떤 이야기가 기억에 남았나요?"))
    )

    [event] = request.events
    assert event.question == "점심 자리에서 어떤 이야기가 기억에 남았나요?"


def test_event_without_a_question_sends_null():
    request = _build(_draft(_event()))

    [event] = request.events
    assert event.question is None


def test_blank_question_becomes_null():
    request = _build(_draft(_event(question="   ")))

    [event] = request.events
    assert event.question is None


def test_question_is_truncated_at_the_column_limit():
    request = _build(_draft(_event(question="가" * 300)))

    [event] = request.events
    assert len(event.question) == 255


def test_serialized_body_matches_the_contract_shape():
    body = _build(_draft(_event())).model_dump(
        by_alias=True, mode="json"
    )

    assert set(body) == {"events"}
    assert set(body["events"][0]) == {
        "eventType",
        "title",
        "subtitle",
        "place",
        "address",
        "startAt",
        "endAt",
        "sourceRawIds",
        "question",
    }
    assert body["events"][0]["startAt"] == "2026-07-22T12:00:00+09:00"


def test_empty_description_becomes_null_subtitle():
    request = _build(_draft(_event(description="   ")))

    assert request.events[0].subtitle is None


def test_duplicate_source_refs_are_deduped_in_order():
    raw_a = fixture_raw_id("result-a")
    raw_b = fixture_raw_id("result-b")
    event = _event(
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=raw_a),
            SourceRef(source_type=EventSourceType.PHOTO, raw_id=raw_b),
            SourceRef(source_type=EventSourceType.CALENDAR, raw_id=raw_a),
        ]
    )

    request = _build(_draft(event))

    assert request.events[0].source_raw_ids == [raw_a, raw_b]


def test_long_title_and_subtitle_are_truncated():
    request = _build(
        _draft(_event(title="가" * 300, description="나" * 300))
    )

    assert len(request.events[0].title) == 255
    assert len(request.events[0].subtitle) == 255


def test_times_are_converted_to_draft_timezone():
    utc_event = _event(
        start_time=datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc),
    )

    body = _build(_draft(utc_event)).model_dump(
        by_alias=True, mode="json"
    )

    assert body["events"][0]["startAt"] == "2026-07-22T12:00:00+09:00"


def test_empty_draft_still_produces_a_request():
    """'결과 없음' 도 확정된 결과다. 저장 요청을 건너뛰지 않는다."""

    assert _build(_draft()).events == []


# --- 장소 (#72) -----------------------------------------------------------------
#
# Repair 가 근거로 확정한 place/address 를 App Server 로 내보낸다. 그전에는 확정해
# 놓고도 계약에 필드가 없어 버려졌다.


def test_place_and_address_are_carried_to_the_contract():
    request = _build(
        _draft(_event(place="두꺼비 감자탕 지산점", address="경기도 오산시 운암로 90"))
    )

    [event] = request.events
    assert event.place == "두꺼비 감자탕 지산점"
    assert event.address == "경기도 오산시 운암로 90"


def test_missing_place_and_address_are_sent_as_null():
    request = _build(_draft(_event()))

    [event] = request.events
    assert event.place is None
    assert event.address is None


def test_blank_place_becomes_null_rather_than_an_empty_string():
    request = _build(_draft(_event(place="   ", address="")))

    [event] = request.events
    assert event.place is None
    assert event.address is None


def test_place_and_address_are_truncated_to_the_column_limit():
    request = _build(
        _draft(_event(place="장" * 300, address="주" * 300))
    )

    [event] = request.events
    assert len(event.place) == 255
    assert len(event.address) == 255


# --- 대표 좌표 (#175) -------------------------------------------------------------
#
# 좌표는 최종 sourceRefs 로 입력을 찾아 코드가 채운다. 사용자가 실제로 있던 지점이고,
# 이동은 도착지다. 장소·주소와 다른 지점을 가리키지 않는다.


def _refs(*items) -> list[SourceRef]:
    types = {"StayItem": EventSourceType.STAY, "MovementItem": EventSourceType.MOVEMENT,
             "PhotoItem": EventSourceType.PHOTO}
    return [SourceRef(source_type=types[type(item).__name__], raw_id=item.raw_id) for item in items]


def _movement(item_id, start_at, end_at, *, end_lat, end_lon, start_lat=37.0, start_lon=127.0):
    return MovementItem(
        raw_id=fixture_raw_id(f"movement-{item_id}"),
        start_at=start_at,
        end_at=end_at,
        start=GeoPlace(latitude=start_lat, longitude=start_lon, address="출발 주소"),
        end=GeoPlace(latitude=end_lat, longitude=end_lon, address=f"도착 주소 {item_id}"),
    )


def _coords(event) -> tuple:
    return (event.latitude, event.longitude)


def test_event_without_location_evidence_sends_no_coordinate_keys():
    """위치 근거가 없는 event 의 body 는 필드를 더하기 전과 같다."""

    body = _build(_draft(_event())).model_dump(by_alias=True, mode="json")

    assert "latitude" not in body["events"][0]
    assert "longitude" not in body["events"][0]
    assert "placeCandidates" not in body["events"][0]


def test_stay_coordinates_are_sent_for_a_stay_event():
    stay = stay_item("a", lat=37.54, lon=126.95, address="서울 마포구 도화동 17-14")
    request = make_request(stays=[stay])

    body = _build(_draft(_event(source_refs=_refs(stay))), request).model_dump(
        by_alias=True, mode="json"
    )

    assert body["events"][0]["latitude"] == 37.54
    assert body["events"][0]["longitude"] == 126.95


def test_movement_uses_the_arrival_of_the_last_movement():
    first = _movement("1", "2026-07-22T09:00:00+09:00", "2026-07-22T09:30:00+09:00",
                      end_lat=37.1, end_lon=127.1)
    last = _movement("2", "2026-07-22T09:40:00+09:00", "2026-07-22T10:00:00+09:00",
                     end_lat=37.2, end_lon=127.2)
    request = make_request(movements=[first, last])
    event = _event(event_type=EventType.MOVEMENT, source_refs=_refs(last, first))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (37.2, 127.2)


def test_movement_without_arrival_coordinates_does_not_fall_back_to_a_waypoint():
    first = _movement("1", "2026-07-22T09:00:00+09:00", "2026-07-22T09:30:00+09:00",
                      end_lat=37.1, end_lon=127.1)
    last = _movement("2", "2026-07-22T09:40:00+09:00", "2026-07-22T10:00:00+09:00",
                     end_lat=None, end_lon=None)
    request = make_request(movements=[first, last])
    event = _event(event_type=EventType.MOVEMENT, source_refs=_refs(first, last))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (None, None)


def test_address_picks_the_point_with_that_address():
    long_stay = stay_item("long", lat=37.0, lon=127.0, address="긴 체류 주소",
                          start="2026-07-22T09:00:00", end="2026-07-22T18:00:00")
    lunch = stay_item("lunch", lat=37.5, lon=127.5, address="점심 주소",
                      start="2026-07-22T12:00:00", end="2026-07-22T12:30:00")
    request = make_request(stays=[long_stay, lunch])
    event = _event(address="점심 주소", source_refs=_refs(long_stay, lunch))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (37.5, 127.5)


def test_address_without_a_matching_point_sends_no_coordinates():
    """좌표가 주소와 다른 곳을 가리키느니 비운다."""

    stay = stay_item("a", lat=37.0, lon=127.0, address="체류 주소")
    request = make_request(stays=[stay])
    event = _event(address="캘린더에만 있는 주소", source_refs=_refs(stay))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (None, None)


def test_place_picks_the_point_that_has_that_name():
    long_stay = stay_item("long", lat=37.0, lon=127.0, address=None, places=["회사 건물"],
                          start="2026-07-22T09:00:00", end="2026-07-22T18:00:00")
    cafe = stay_item("cafe", lat=37.5, lon=127.5, address=None, places=["카페 OO"],
                     start="2026-07-22T12:00:00", end="2026-07-22T12:30:00")
    request = make_request(stays=[long_stay, cafe])
    event = _event(place="카페 OO", source_refs=_refs(long_stay, cafe))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (37.5, 127.5)


def test_without_address_or_place_the_longest_stay_is_the_point():
    short = stay_item("short", lat=37.5, lon=127.5, address=None,
                      start="2026-07-22T12:00:00", end="2026-07-22T12:30:00")
    long_stay = stay_item("long", lat=37.0, lon=127.0, address=None,
                          start="2026-07-22T09:00:00", end="2026-07-22T18:00:00")
    request = make_request(stays=[short, long_stay])
    event = _event(source_refs=_refs(short, long_stay))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (37.0, 127.0)


def test_photo_moment_prefers_the_photo_point():
    stay = stay_item("a", lat=37.0, lon=127.0, address=None)
    photo = photo_item("p", lat=37.5, lon=127.5)
    request = make_request(stays=[stay], photos=[photo])
    event = _event(event_type=EventType.PHOTO_MOMENT, source_refs=_refs(stay, photo))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (37.5, 127.5)


def test_photo_without_location_gives_no_coordinates():
    photo = photo_item("p")
    request = make_request(photos=[photo])
    event = _event(event_type=EventType.PHOTO_MOMENT, source_refs=_refs(photo))

    [result] = _build(_draft(event), request).events

    assert _coords(result) == (None, None)


def test_coordinates_follow_the_final_source_refs():
    """Repair 가 근거를 바꾸면 좌표도 바뀐 근거를 따른다."""

    before = stay_item("before", lat=37.0, lon=127.0, address=None)
    after = stay_item("after", lat=37.5, lon=127.5, address=None)
    request = make_request(stays=[before, after])

    [result] = _build(_draft(_event(source_refs=_refs(after))), request).events

    assert _coords(result) == (37.5, 127.5)


def _result_event(**overrides) -> dict:
    body = dict(
        eventType="MEAL",
        title="점심",
        startAt="2026-07-22T12:00:00+09:00",
        endAt="2026-07-22T13:00:00+09:00",
        sourceRawIds=[fixture_raw_id("result-1")],
    )
    body.update(overrides)
    return body


def test_contract_rejects_a_single_coordinate_axis():
    """App Server 는 한쪽만 온 결과를 통째로 거절한다. 보내기 전에 막는다."""

    with pytest.raises(ValidationError):
        TimelineResultEvent.model_validate(_result_event(latitude=37.5))


def test_contract_rejects_out_of_range_coordinates():
    with pytest.raises(ValidationError):
        TimelineResultEvent.model_validate(_result_event(latitude=91.0, longitude=127.0))


def test_place_candidates_serialize_as_named_objects():
    event = TimelineResultEvent.model_validate(
        _result_event(placeCandidates=[{"name": "성수동 카페"}, {"name": "△△공원"}])
    )

    body = event.model_dump(by_alias=True, mode="json")

    assert body["placeCandidates"] == [{"name": "성수동 카페"}, {"name": "△△공원"}]
