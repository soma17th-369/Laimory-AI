"""기억한 장소로 후보 보강 (#192, v4).

User Memory `notablePlacePoints` 의 장소가 다음 타임라인의 체류·이동 도착 지점 250m 안이면 그 지점의 후보가
된다. 같은 이름이라도 250m 밖이면 아니다. 위치 뼈대는 기억을 보지 않는다.
"""

from datetime import datetime, timedelta, timezone

from app.schemas import (
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    UserMemory,
)
from app.schemas.user_memory import NotablePlacePoint
from app.services.location_skeleton import build_location_skeleton
from app.services.place_memory import PlaceMemory, add_remembered_places
from app.services.place_slots import confirm_place_slots, fill_place_slots
from app.services.timeline_result import build_result_request
from tests.fixtures.requests import calendar_item, make_request, movement_item, stay_item

_KST = timezone(timedelta(hours=9))

#: 위도 1도는 약 111.2km 다. 0.00224도 ≈ 249m, 0.00226도 ≈ 251m.
_BASE = (37.5445, 127.0559)
_INSIDE = (_BASE[0] + 0.00224, _BASE[1])
_OUTSIDE = (_BASE[0] + 0.00226, _BASE[1])
_FAR = (35.1, 129.0)


def _point(name: str, at: tuple[float, float]) -> NotablePlacePoint:
    return NotablePlacePoint(name=name, latitude=at[0], longitude=at[1])


def _memory(*points: NotablePlacePoint, names: tuple[str, ...] = ()) -> UserMemory:
    return UserMemory(
        notable_places=[*names, *(point.name for point in points)],
        notable_place_points=list(points),
    )


def _stay(label: str, at=_BASE, places=None, start="2026-06-20T12:00:00", end="2026-06-20T13:00:00"):
    return stay_item(label, lat=at[0], lon=at[1], places=places or ["OO빌딩"], start=start, end=end)


def _event(stay, **overrides) -> TimelineEventDraft:
    values = dict(
        client_event_id="event-001",
        event_type=EventType.STAY,
        title="시간을 보냄",
        description="{place}에서 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 12, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 13, 0, tzinfo=_KST),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=stay.raw_id)],
    )
    values.update(overrides)
    return TimelineEventDraft(**values)


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


# --- 후보 보강 ------------------------------------------------------------------


def test_a_remembered_place_within_250m_joins_the_stay_candidates():
    stay = _stay("inside")
    request = make_request(stays=[stay], user_memory=_memory(_point("OO커피 성수점", _INSIDE)))

    add_remembered_places(request)

    assert stay.places == ["OO빌딩", "OO커피 성수점"], "원래 후보 뒤에 붙는다."
    [added] = [point for point in stay.place_points if point.remembered]
    assert (added.name, added.latitude, added.longitude) == ("OO커피 성수점", *_INSIDE)


def test_a_remembered_place_past_250m_is_not_a_candidate():
    stay = _stay("outside")
    request = make_request(stays=[stay], user_memory=_memory(_point("OO커피 성수점", _OUTSIDE)))

    add_remembered_places(request)

    assert stay.places == ["OO빌딩"]


def test_the_same_name_far_away_does_not_join():
    """이름이 같은 다른 지점(다른 동네의 같은 상호)은 좌표로 가른다."""

    stay = _stay("far")
    request = make_request(stays=[stay], user_memory=_memory(_point("OO커피", _FAR)))

    add_remembered_places(request)

    assert stay.places == ["OO빌딩"]


def test_a_name_already_in_the_candidates_is_not_added_twice():
    stay = _stay("same", places=["OO빌딩", "OO커피 성수점"])
    request = make_request(stays=[stay], user_memory=_memory(_point("OO커피성수점", _INSIDE)))

    add_remembered_places(request)

    assert stay.places == ["OO빌딩", "OO커피 성수점"]
    assert not any(point.remembered for point in stay.place_points)


def test_nearer_places_come_first_and_several_places_in_one_building_all_join():
    stay = _stay("building")
    request = make_request(
        stays=[stay],
        user_memory=_memory(_point("OO약국", _INSIDE), _point("OO타워", _BASE)),
    )

    add_remembered_places(request)

    assert stay.places == ["OO빌딩", "OO타워", "OO약국"]


def test_the_arrival_of_a_movement_gets_remembered_places_too():
    movement = movement_item("arrive").model_copy(
        update={"end": GeoPlace(latitude=_BASE[0], longitude=_BASE[1], places=["OO역"])}
    )
    request = make_request(movements=[movement], user_memory=_memory(_point("OO타워", _INSIDE)))

    add_remembered_places(request)

    assert movement.end.places == ["OO역", "OO타워"]


def test_nothing_changes_without_remembered_coordinates():
    stay = _stay("legacy")
    request = make_request(stays=[stay], user_memory=_memory(names=("OO커피 성수점",)))

    add_remembered_places(request)

    assert stay.places == ["OO빌딩"], "좌표 없는 예전 이름(#181)은 후보를 만들지 않는다."


# --- 전에 고른 곳 ------------------------------------------------------------------


def test_visited_needs_the_remembered_point_nearby_but_a_legacy_name_only_needs_the_name():
    memory = PlaceMemory.of(_memory(_point("OO커피", _FAR), names=("OO서점",)))

    assert memory.visited(["OO커피", "OO서점", "OO빌딩"], [_BASE]) == ["OO서점"]
    assert memory.visited(["OO커피"], [_FAR]) == ["OO커피"]


# --- 위치 뼈대는 그대로 -------------------------------------------------------------


def test_the_skeleton_splits_the_day_the_same_way_with_or_without_memory():
    """기억 이름이 같은 장소 판정에 들어가면 250m~500m 떨어진 두 지점이 같은 장소가 된다."""

    def stays():
        return [
            stay_item(
                "a", lat=_BASE[0], lon=_BASE[1], places=["A빌딩"], address="서울 성동구 성수동1가 1",
                start="2026-06-20T10:00:00", end="2026-06-20T11:00:00",
            ),
            stay_item(
                "b", lat=_BASE[0] + 0.004, lon=_BASE[1], places=["B빌딩"], address="서울 성동구 성수동1가 9",
                start="2026-06-20T13:00:00", end="2026-06-20T14:00:00",
            ),
        ]

    plain = make_request(stays=stays())
    remembering = make_request(
        stays=stays(),
        user_memory=_memory(_point("OO타워", (_BASE[0] + 0.002, _BASE[1]))),
    )
    add_remembered_places(remembering)

    def shape(request):
        skeleton = build_location_skeleton(request)
        return [
            (segment.kind, segment.start, segment.end, [r.raw_id for r in segment.records])
            for segment in skeleton.segments
        ] + [(empty.kind, empty.start, empty.end, empty.same_place) for empty in skeleton.empty_times]

    assert shape(plain) == shape(remembering)
    assert False in [empty.same_place for empty in build_location_skeleton(plain).empty_times], (
        "445m 떨어진 두 체류는 다른 곳이다. 둘 다 222m 거리의 기억 장소가 붙어도 그대로다."
    )
    skeleton = build_location_skeleton(remembering)
    assert all("OO타워" in segment.places for segment in skeleton.segments), "구간 후보에는 붙는다."
    assert all(
        "OO타워" not in record.spot.places for segment in skeleton.segments for record in segment.records
    ), "같은 장소 판정에 쓰는 지점 이름에는 없다."


# --- 후보 순서와 결과 저장 -----------------------------------------------------------


def test_a_remembered_place_new_to_the_input_is_saved_with_its_remembered_coordinates():
    """원래 입력에 없던 이름이라고 결과 저장 직전 대조에서 빠지지 않는다."""

    stay = _stay("saved")
    request = make_request(stays=[stay], user_memory=_memory(_point("OO커피 성수점", _INSIDE)))
    add_remembered_places(request)
    event = _event(stay, place_candidates=["OO빌딩", "OO커피 성수점"])
    draft = _draft(event)

    confirm_place_slots(draft, request, None)
    fill_place_slots(draft, request)
    [saved] = build_result_request(draft, request).model_dump(by_alias=True, mode="json")["events"]

    assert event.place == "OO커피 성수점", "위치뿐인 체류는 전에 고른 곳이 1번이다(#181)."
    assert saved["placeCandidates"][0] == {
        "name": "OO커피 성수점",
        "latitude": _INSIDE[0],
        "longitude": _INSIDE[1],
    }


def test_the_same_name_far_away_does_not_move_up():
    """좌표가 저장된 이름은 그 event 지점 250m 안일 때만 전에 고른 곳이다."""

    stay = _stay("far-order", places=["OO빌딩", "OO커피"])
    request = make_request(stays=[stay], user_memory=_memory(_point("OO커피", _FAR)))
    add_remembered_places(request)
    event = _event(stay, place_candidates=["OO빌딩", "OO커피"])

    confirm_place_slots(_draft(event), request, None)

    assert event.place_candidates == ["OO빌딩", "OO커피"]


def test_a_remembered_place_comes_first_in_a_calendar_meeting():
    """일정에 장소 칸이 없는 회의. 운영 기록에서 Timeline 이 그 장소를 후보 목록에 두고도 5개 안에 넣지 않았다.

    전에 고른 곳은 사진·일정·알림이 있는 event 에서도 근거가 가리키는 이름 다음이다(사용자 결정 2026-11-03).
    """

    stay = _stay("meeting", places=["OO빌딩 주차장", "OO의원", "OO사무소", "OO우체국", "OO지원", "OO센터"])
    meeting = calendar_item("m", "팀 정기 회의", start="2026-06-20T12:00:00", end="2026-06-20T13:00:00")
    request = make_request(
        stays=[stay], calendars=[meeting], user_memory=_memory(_point("OO센터", _INSIDE))
    )
    add_remembered_places(request)
    event = _event(
        stay,
        event_type=EventType.MEETING,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=stay.raw_id),
            SourceRef(source_type=EventSourceType.CALENDAR, raw_id=meeting.raw_id),
        ],
        place_candidates=["OO사무소", "OO의원", "OO우체국", "OO지원"],
    )

    confirm_place_slots(_draft(event), request, None)

    assert event.place_candidates[:2] == ["OO센터", "OO사무소"]


def test_the_remembered_place_nearer_the_event_comes_first():
    """회의가 저녁 체류까지 근거로 가져 그 근처 식당도 전에 간 곳으로 올라온다. event 위치(가장 오래 겹친
    체류)에 가까운 곳이 1번이다(사용자 결정 2026-11-03)."""

    office = _stay("office", at=_BASE, places=["OO타워"], start="2026-06-20T12:00:00", end="2026-06-20T18:00:00")
    evening_at = (_BASE[0] + 0.004, _BASE[1])
    evening = _stay("evening", at=evening_at, places=["OO치킨"], start="2026-06-20T20:00:00", end="2026-06-20T21:00:00")
    request = make_request(
        stays=[office, evening],
        user_memory=_memory(_point("OO치킨", evening_at), _point("OO센터", _BASE)),
    )
    add_remembered_places(request)
    event = _event(
        office,
        event_type=EventType.MEETING,
        start_time=datetime(2026, 6, 20, 12, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 21, 0, tzinfo=_KST),
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id),
            SourceRef(source_type=EventSourceType.STAY, raw_id=evening.raw_id),
        ],
        place_candidates=["OO치킨", "OO타워"],
    )

    confirm_place_slots(_draft(event), request, None)

    assert event.place_candidates[:2] == ["OO센터", "OO치킨"]
