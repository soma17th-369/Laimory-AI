"""위치 뼈대 검증 (#167).

v3 Location 프롬프트의 규칙을 코드로 옮긴 것이므로, 계획의 「Location 규칙 이식표」 줄마다
테스트를 둔다(테스트 이름 앞의 `L번호`). 거리 기준(250m·1km)은 사용자 결정이다(처음 300m, 2026-10-30 에 250m).

좌표는 위도 0.001 이 약 111m 다. 0.002 는 같은 장소(250m 이내), 0.005 는 250m~1km,
0.02 는 다른 장소(1km 초과)다. 250m~1km 라도 두 곳에 각각 30분 넘게 머물렀으면 다른 방문이다(#201).
"""

import math
from datetime import datetime, timedelta

import pytest

from app.schemas import GeoPlace, MovementItem, TimeWindow
from app.services import location_skeleton
from app.services.location_skeleton import (
    EmptyTimeKind,
    SegmentKind,
    build_location_skeleton,
)
from app.services.place_resolver import unique_place_names
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

_BASE_LAT = 37.5
_BASE_LON = 127.0


def _t(clock: str) -> str:
    """`HH:MM` 이나 `HH:MM:SS` 를 그날의 시각 문자열로."""

    return f"2026-06-20T{clock}" if clock.count(":") == 2 else f"2026-06-20T{clock}:00"


def _stay(label, start, end, *, lat=_BASE_LAT, lon=_BASE_LON, places=None, address=None):
    return stay_item(
        label,
        lat=lat,
        lon=lon,
        start=_t(start),
        end=_t(end),
        places=places if places is not None else [],
        address=address or f"주소 {label}",
    )


def _move(
    label,
    start,
    end,
    *,
    from_lat=_BASE_LAT,
    to_lat=_BASE_LAT + 0.02,
    lon=_BASE_LON,
    transports=("IN_VEHICLE",),
    distance=None,
    to_places=None,
):
    return MovementItem(
        raw_id=fixture_raw_id(f"movement-{label}"),
        start_at=_t(start),
        end_at=_t(end),
        start=GeoPlace(latitude=from_lat, longitude=lon, address=f"출발 {label}"),
        end=GeoPlace(
            latitude=to_lat, longitude=lon, address=f"도착 {label}", places=to_places or []
        ),
        transports=list(transports),
        distance_meters=distance,
    )


def _skeleton(stays=(), movements=()):
    return build_location_skeleton(make_request(stays=list(stays), movements=list(movements)))


def _kinds(skeleton):
    return [(segment.kind, len(segment.records)) for segment in skeleton.segments]


# --- 위치 기록이 없거나 빈 시간 ---------------------------------------------------


def test_no_location_records_is_not_a_failure():
    skeleton = _skeleton()

    assert not skeleton.has_location
    assert skeleton.segments == []
    assert skeleton.empty_times == []
    assert skeleton.raw_ids() == []


def test_a_few_records_cover_only_part_of_the_day():
    """기록 몇 건뿐이어도 같은 규칙이다. 나머지 시간은 빈 시간으로 드러난다."""

    skeleton = _skeleton(stays=[_stay("a", "13:00", "15:00")])

    assert _kinds(skeleton) == [(SegmentKind.STAY, 1)]
    leading, trailing = skeleton.empty_times
    assert leading.kind is EmptyTimeKind.COLLECTION_GAP and leading.before is None
    assert trailing.kind is EmptyTimeKind.COLLECTION_GAP and trailing.after is None
    assert skeleton.first_observed_at.hour == 13
    assert skeleton.last_observed_at.hour == 15


def test_empty_time_is_not_filled_by_the_neighboring_stays():
    """일부 시간만 빌 때 앞뒤 체류를 늘려 메우지 않는다."""

    skeleton = _skeleton(
        stays=[_stay("a", "09:00", "10:00"), _stay("b", "14:00", "15:00", lat=_BASE_LAT + 0.005)]
    )

    first, second = skeleton.segments
    assert (first.end.hour, second.start.hour) == (10, 14)
    [gap] = [e for e in skeleton.empty_times if e.before and e.after]
    assert (gap.start.hour, gap.end.hour) == (10, 14)


# --- 기록 보존 (L1·L2·L4) ----------------------------------------------------------


def test_L1_every_record_lands_exactly_once():
    stays = [_stay("a", "08:00", "09:00"), _stay("b", "09:05", "09:10"), _stay("c", "20:00", "20:05")]
    movements = [_move("m", "09:10", "09:40")]
    undated = stay_item("undated", start="시각 아님", end=None)

    skeleton = _skeleton(stays=[*stays, undated], movements=movements)

    expected = [item.raw_id for item in [*stays, undated, *movements]]
    assert sorted(skeleton.raw_ids()) == sorted(expected)
    assert len(skeleton.raw_ids()) == len(set(skeleton.raw_ids()))
    assert undated.raw_id in skeleton.unplaced_raw_ids


def test_L1_record_entirely_outside_the_window_is_kept_as_unplaced():
    outside = stay_item("old", start="2026-06-19T08:00:00", end="2026-06-19T09:00:00")

    skeleton = _skeleton(stays=[outside])

    assert skeleton.unplaced_raw_ids == [outside.raw_id]
    assert not skeleton.has_location


def test_L4_segment_time_is_clipped_to_the_window():
    overnight = stay_item("night", start="2026-06-19T22:00:00", end="2026-06-20T07:00:00")

    [segment] = _skeleton(stays=[overnight]).segments

    assert (segment.start.day, segment.start.hour) == (20, 0)
    assert segment.end.hour == 7


def test_L3_overlapping_records_do_not_make_overlapping_segments():
    stay = _stay("a", "09:00", "10:10")
    movement = _move("m", "10:00", "10:40", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.05)
    after = _stay("b", "10:30", "12:00", lat=_BASE_LAT + 0.05)

    segments = _skeleton(stays=[stay, after], movements=[movement]).segments

    for previous, segment in zip(segments, segments[1:]):
        assert previous.end <= segment.start


# --- 짧은 체류와 여정 (L5~L10·L12~L14) ------------------------------------------------


@pytest.mark.parametrize("resumed", ["10:14", "10:15", "10:37", "13:00"])
def test_L5_movements_are_one_journey_across_any_gap(resumed):
    """체류가 잡히지 않았으면 수집이 끊긴 것으로 보고 잇는다(#180, 사용자 결정 2026-11-01)."""

    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    second = _move("2", resumed, "13:30", from_lat=_BASE_LAT + 0.05, to_lat=_BASE_LAT + 0.08)

    skeleton = _skeleton(movements=[first, second])

    assert _kinds(skeleton) == [(SegmentKind.MOVEMENT, 2)]
    [journey] = skeleton.segments
    assert (journey.start.hour, journey.end.hour) == (9, 13)
    assert (journey.origin.latitude, journey.destination.latitude) == (_BASE_LAT, _BASE_LAT + 0.08)
    assert [e for e in skeleton.empty_times if e.before and e.after] == []


def test_L5_a_gap_under_20_minutes_at_the_same_place_is_still_the_journey():
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    second = _move("2", "09:49", "10:15", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04)

    assert _kinds(_skeleton(movements=[first, second])) == [(SegmentKind.MOVEMENT, 2)]


@pytest.mark.parametrize("resumed", ["09:50", "10:00", "10:15"])
def test_L5_a_gap_of_20_minutes_at_the_same_place_is_a_stop(resumed):
    """앞이 도착한 곳에서 뒤가 출발했으면 체류 기록이 없어도 머문 것이다."""

    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    second = _move("2", resumed, "10:45", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04)

    assert _kinds(_skeleton(movements=[first, second])) == [
        (SegmentKind.MOVEMENT, 1),
        (SegmentKind.MOVEMENT, 1),
    ]


def test_L5_coming_home_and_leaving_next_morning_is_not_one_journey():
    """귀가 뒤 밤새 수집이 끊기면 이동 둘만 남는다. 이으면 밤 전체가 이동이 된다."""

    home = _BASE_LAT + 0.02
    back = _move("back", "21:00", "21:30", to_lat=home)
    out = MovementItem(
        raw_id=fixture_raw_id("movement-out"),
        start_at="2026-06-21T08:00:00",
        end_at="2026-06-21T08:30:00",
        start=GeoPlace(latitude=home, longitude=_BASE_LON),
        end=GeoPlace(latitude=_BASE_LAT, longitude=_BASE_LON),
        transports=["IN_VEHICLE"],
    )
    request = make_request(
        movements=[back, out],
        window=TimeWindow(start="2026-06-20T00:00:00+09:00", end="2026-06-22T00:00:00+09:00"),
    )

    skeleton = build_location_skeleton(request)

    assert _kinds(skeleton) == [(SegmentKind.MOVEMENT, 1), (SegmentKind.MOVEMENT, 1)]
    [gap] = [e for e in skeleton.empty_times if e.before and e.after]
    assert gap.kind is EmptyTimeKind.COLLECTION_GAP and gap.same_place is True


def test_L5_a_short_stay_past_a_gap_stays_off_the_journey():
    """공백 너머의 짧은 체류를 이동에 붙이면 구간의 도착지가 어긋난다. 그 자리는 45분 기준 그대로다."""

    movement = _move("m", "09:00", "09:30")
    far_short = _stay("far", "11:00", "11:10", lat=_BASE_LAT + 0.5)

    skeleton = _skeleton(stays=[far_short], movements=[movement])

    assert _kinds(skeleton) == [(SegmentKind.MOVEMENT, 1)]
    assert [record.raw_id for record in skeleton.isolated] == [far_short.raw_id]


def test_a_movement_after_a_stay_and_a_gap_left_from_the_stay():
    """동대구역 체류 → 끊김 → 세종에서 시작한 이동. 사이에 체류 정보가 없으니 역에서 출발한 이동이다."""

    station = _stay("station", "08:44", "09:14")
    train = _move("train", "10:12", "12:03", from_lat=_BASE_LAT + 1.2, to_lat=_BASE_LAT + 2.0)

    skeleton = _skeleton(stays=[station], movements=[train])

    stay, journey = skeleton.segments
    assert (stay.kind, journey.kind) == (SegmentKind.STAY, SegmentKind.MOVEMENT)
    assert (journey.start.hour, journey.start.minute) == (9, 14), "체류가 끝난 때 출발했다."
    assert stay.end == journey.start
    assert journey.origin.address == "주소 station"
    assert skeleton.departures[train.raw_id].address == "주소 station"
    assert skeleton.record_spans()[train.raw_id][0] == journey.start
    assert [e for e in skeleton.empty_times if e.before and e.after] == []
    assert sorted(skeleton.raw_ids()) == sorted([station.raw_id, train.raw_id])


@pytest.mark.parametrize("apart", [0.002, 0.005], ids=["같은 장소", "250m~1km"])
def test_a_movement_within_1km_of_the_stay_keeps_its_own_start(apart):
    """그 안이면 체류를 떠났는지 모른다. 빈 시간으로 남는다."""

    stay = _stay("office", "09:00", "12:00")
    walk = _move("m", "13:30", "14:00", from_lat=_BASE_LAT + apart, to_lat=_BASE_LAT + 0.05)

    skeleton = _skeleton(stays=[stay], movements=[walk])

    assert (skeleton.segments[-1].start.hour, skeleton.segments[-1].start.minute) == (13, 30)
    assert skeleton.segments[-1].origin.address == "출발 m"
    assert skeleton.departures == {}
    [gap] = [e for e in skeleton.empty_times if e.before and e.after]
    assert gap.kind is EmptyTimeKind.COLLECTION_GAP


def test_a_movement_right_after_a_stay_keeps_its_own_origin():
    """기록이 끊기지 않았으면 입력의 출발지가 맞다."""

    stay = _stay("office", "09:00", "12:00")
    movement = _move("m", "12:00", "13:00", from_lat=_BASE_LAT + 0.5, to_lat=_BASE_LAT + 0.8)

    skeleton = _skeleton(stays=[stay], movements=[movement])

    assert skeleton.departures == {}
    assert skeleton.segments[-1].origin.address == "출발 m"


def test_movements_with_many_short_transfers_are_one_journey():
    """환승마다 기록이 갈려도 여정은 하나고, 입력 rawId 는 빠지거나 겹치지 않는다."""

    records = [
        _move("1", "09:00", "09:20", to_lat=_BASE_LAT + 0.02),
        _stay("t1", "09:20", "09:30", lat=_BASE_LAT + 0.02),
        _move("2", "09:30", "09:50", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04),
        _stay("t2", "09:50", "10:05", lat=_BASE_LAT + 0.04),
        _move("3", "10:05", "10:30", from_lat=_BASE_LAT + 0.04, to_lat=_BASE_LAT + 0.06),
    ]
    stays = [r for r in records if not isinstance(r, MovementItem)]
    movements = [r for r in records if isinstance(r, MovementItem)]

    skeleton = _skeleton(stays=stays, movements=movements)

    assert _kinds(skeleton) == [(SegmentKind.MOVEMENT, 5)]
    assert sorted(skeleton.segments[0].raw_ids) == sorted(r.raw_id for r in records)
    assert skeleton.isolated == [] and skeleton.unplaced_raw_ids == []


@pytest.mark.parametrize(
    ("stay_end", "expected"),
    [
        ("2026-06-20T09:49:59", [(SegmentKind.MOVEMENT, 3)]),
        (
            "2026-06-20T09:50:00",
            [(SegmentKind.MOVEMENT, 1), (SegmentKind.STAY, 1), (SegmentKind.MOVEMENT, 1)],
        ),
        (
            "2026-06-20T09:50:01",
            [(SegmentKind.MOVEMENT, 1), (SegmentKind.STAY, 1), (SegmentKind.MOVEMENT, 1)],
        ),
    ],
    ids=["19분59초", "정확히 20분", "20분 초과"],
)
def test_20_minutes_is_where_a_stay_cuts_the_journey(stay_end, expected):
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    stay = stay_item(
        "station",
        lat=_BASE_LAT + 0.02,
        lon=_BASE_LON,
        start="2026-06-20T09:30:00",
        end=stay_end,
        places=[],
        address="주소 역",
    )
    second = MovementItem(
        raw_id=fixture_raw_id("movement-2"),
        start_at=stay_end,
        end_at="2026-06-20T10:30:00",
        start=GeoPlace(latitude=_BASE_LAT + 0.02, longitude=_BASE_LON),
        end=GeoPlace(latitude=_BASE_LAT + 0.06, longitude=_BASE_LON),
        transports=["IN_VEHICLE"],
    )

    assert _kinds(_skeleton(stays=[stay], movements=[first, second])) == expected


def test_L6_short_stay_next_to_a_movement_goes_to_the_movement():
    movement = _move("m", "09:00", "09:30")
    short = _stay("transfer", "09:30", "09:45", lat=_BASE_LAT + 0.02)
    home = _stay("home", "12:00", "18:00", lat=_BASE_LAT + 0.06)

    skeleton = _skeleton(stays=[short, home], movements=[movement])

    journey = skeleton.segments[0]
    assert journey.kind is SegmentKind.MOVEMENT
    assert short.raw_id in journey.raw_ids


def test_L6_short_stay_between_a_movement_and_a_long_stay_goes_to_the_movement():
    movement = _move("m", "09:00", "09:30")
    short = _stay("lobby", "09:31", "09:40", lat=_BASE_LAT + 0.02)
    office = _stay("office", "09:41", "18:00", lat=_BASE_LAT + 0.0201)

    skeleton = _skeleton(stays=[short, office], movements=[movement])

    assert _kinds(skeleton) == [(SegmentKind.MOVEMENT, 2), (SegmentKind.STAY, 1)]


def test_L7_short_stay_continuous_with_another_stay_joins_it():
    long_stay = _stay("main", "09:00", "12:00")
    short = _stay("corner", "12:05", "12:15", lat=_BASE_LAT + 0.001)

    assert _kinds(_skeleton(stays=[long_stay, short])) == [(SegmentKind.STAY, 2)]


def test_L8_lone_short_stay_is_an_isolated_record():
    lone = _stay("lone", "15:00", "15:10")

    skeleton = _skeleton(stays=[lone])

    assert skeleton.segments == []
    assert [record.raw_id for record in skeleton.isolated] == [lone.raw_id]
    assert skeleton.has_location


def test_L8_short_movement_connected_to_nothing_is_an_isolated_record():
    movement = _move("m", "15:00", "15:10")

    skeleton = _skeleton(movements=[movement])

    assert [record.raw_id for record in skeleton.isolated] == [movement.raw_id]


def test_L8_short_movement_connected_to_a_stay_is_a_segment():
    stay = _stay("a", "09:00", "11:00")
    movement = _move("m", "11:05", "11:15")

    assert _kinds(_skeleton(stays=[stay], movements=[movement])) == [
        (SegmentKind.STAY, 1),
        (SegmentKind.MOVEMENT, 1),
    ]


def test_L9_long_stay_between_movements_is_split_out():
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    stay = _stay("station", "09:30", "09:55", lat=_BASE_LAT + 0.02)
    second = _move("2", "09:55", "10:30", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04)

    assert _kinds(_skeleton(stays=[stay], movements=[first, second])) == [
        (SegmentKind.MOVEMENT, 1),
        (SegmentKind.STAY, 1),
        (SegmentKind.MOVEMENT, 1),
    ]


def test_L10_no_exception_for_a_nameless_transfer_stay():
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    transfer = _stay("transfer", "09:30", "09:52", lat=_BASE_LAT + 0.02, places=[], address=None)
    second = _move("2", "09:52", "10:30", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04)

    skeleton = _skeleton(stays=[transfer], movements=[first, second])

    assert [segment.kind for segment in skeleton.segments] == [
        SegmentKind.MOVEMENT,
        SegmentKind.STAY,
        SegmentKind.MOVEMENT,
    ]


def test_L12_L13_L14_connected_movements_are_one_journey_from_origin_to_final_destination():
    first = _move("1", "09:00", "09:20", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.02)
    transfer = _stay("transfer", "09:20", "09:30", lat=_BASE_LAT + 0.02)
    second = _move(
        "2", "09:30", "10:00", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.05,
        to_places=["강남역"],
    )

    [journey] = _skeleton(stays=[transfer], movements=[first, second]).segments

    assert journey.kind is SegmentKind.MOVEMENT
    assert journey.raw_ids == [first.raw_id, transfer.raw_id, second.raw_id]
    assert journey.origin.address == "출발 1"
    assert journey.destination.address == "도착 2"
    assert journey.places == ["강남역"]
    assert (journey.latitude, journey.address) == (_BASE_LAT + 0.05, "도착 2")


def test_L15_journey_distance_sums_the_movement_distances():
    first = _move("1", "09:00", "09:20", distance=3000.0)
    second = _move("2", "09:25", "09:50", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.05, distance=4000.0)

    [journey] = _skeleton(movements=[first, second]).segments

    assert journey.distance_meters == 7000.0


def test_L16_region_change_compares_origin_and_destination_names():
    movement = _move("m", "09:00", "10:00")

    [journey] = _skeleton(movements=[movement]).segments

    assert journey.region_changed is True


# --- 체류끼리 묶기 (L11·L17~L20) — 거리 기준은 사용자 결정 ------------------------------


def test_L18_same_place_within_250m_is_one_stay_however_long_the_gap():
    morning = _stay("morning", "09:00", "11:00")
    evening = _stay("evening", "15:00", "18:00", lat=_BASE_LAT + 0.002)  # 약 222m — 같은 아파트 실측 212m 를 담는다

    [segment] = _skeleton(stays=[morning, evening]).segments

    assert segment.kind is SegmentKind.STAY
    [gap] = segment.internal_gaps
    assert (gap.start.hour, gap.end.hour) == (11, 15)


def test_L18_beyond_250m_a_long_gap_means_another_place():
    """같은 장소 기준은 250m 다(사용자 결정 2026-10-30, 300m 는 너무 크다). 약 278m 떨어진 체류가
    4시간 끊겼으면 다른 장소다. 끊기지 않고 이어졌으면(45분 미만) 1km 까지 한 장소다."""

    morning = _stay("morning", "09:00", "11:00")
    evening = _stay("evening", "15:00", "18:00", lat=_BASE_LAT + 0.0025)

    assert len([s for s in _skeleton(stays=[morning, evening]).segments if s.kind is SegmentKind.STAY]) == 2


def test_L18_addresses_that_differ_but_sit_together_are_the_same_place():
    """같은 건물이 `도화동 17-13`·`마포대로 83` 처럼 다른 주소로 온다(trace 3fc24b7d)."""

    first = _stay("a", "11:25", "11:54", address="서울특별시 마포구 마포대로 83")
    second = _stay(
        "b", "12:00", "22:00", lat=_BASE_LAT + 0.0006, address="서울 마포구 도화동 17-14"
    )

    assert _kinds(_skeleton(stays=[first, second])) == [(SegmentKind.STAY, 2)]


def test_L11_more_than_1km_apart_is_always_another_place():
    """이동 기록이 없어도 나누고, 그 사이를 기록되지 않은 이동으로 둔다(trace e3c19544)."""

    first = _stay("a", "16:59", "17:38")
    second = _stay("b", "17:50", "19:37", lat=_BASE_LAT + 0.1)

    skeleton = _skeleton(stays=[first, second])

    assert _kinds(skeleton) == [(SegmentKind.STAY, 1), (SegmentKind.STAY, 1)]
    [moved] = [e for e in skeleton.empty_times if e.kind is EmptyTimeKind.UNRECORDED_MOVEMENT]
    assert (moved.start.minute, moved.end.minute) == (38, 50)
    assert moved.same_place is False


def test_L11_far_apart_with_a_long_gap_is_still_an_unrecorded_movement():
    first = _stay("a", "09:00", "10:00")
    second = _stay("b", "14:00", "15:00", lat=_BASE_LAT + 0.1)

    skeleton = _skeleton(stays=[first, second])

    kinds = [e.kind for e in skeleton.empty_times if e.before and e.after]
    assert kinds == [EmptyTimeKind.UNRECORDED_MOVEMENT]


def test_L17_continuous_stays_250m_to_1km_apart_are_one_stay_with_the_longest_first():
    """캠퍼스·단지 안의 여러 지점. 한쪽이 30분 이하로 들른 곳이면 한 구간이고, 대표 지점은 가장 오래
    머문 체류다. 두 곳에 각각 30분 넘게 머물렀으면 다른 방문이다(#201, 아래 「별도 방문」)."""

    building = _stay("building", "09:00", "09:30", places=["공학관"])
    library = _stay("library", "09:45", "13:00", lat=_BASE_LAT + 0.005, places=["도서관"])

    [segment] = _skeleton(stays=[building, library]).segments

    assert segment.places == ["도서관", "공학관"]
    assert segment.latitude == _BASE_LAT + 0.005
    assert segment.address == "주소 library"


def test_L19_250m_to_1km_apart_after_a_long_gap_is_split_by_an_empty_time():
    first = _stay("a", "09:00", "10:00")
    second = _stay("b", "11:00", "12:00", lat=_BASE_LAT + 0.005)

    skeleton = _skeleton(stays=[first, second])

    assert _kinds(skeleton) == [(SegmentKind.STAY, 1), (SegmentKind.STAY, 1)]
    [gap] = [e for e in skeleton.empty_times if e.before and e.after]
    assert gap.kind is EmptyTimeKind.COLLECTION_GAP
    assert gap.same_place is False


def test_L20_a_movement_between_same_place_stays_keeps_them_apart():
    """나갔다 같은 곳으로 돌아온 것이다. 이동은 예외 없이 나눈다."""

    first = _stay("home-1", "08:00", "10:00")
    out = _move("out", "10:00", "11:00", from_lat=_BASE_LAT, to_lat=_BASE_LAT)
    second = _stay("home-2", "11:00", "18:00")

    assert _kinds(_skeleton(stays=[first, second], movements=[out])) == [
        (SegmentKind.STAY, 1),
        (SegmentKind.MOVEMENT, 1),
        (SegmentKind.STAY, 1),
    ]


def test_a_stay_at_another_place_between_same_place_stays_keeps_them_apart():
    first = _stay("home-1", "08:00", "10:00")
    elsewhere = _stay("far", "10:30", "12:00", lat=_BASE_LAT + 0.1)
    second = _stay("home-2", "12:30", "18:00")

    assert len(_skeleton(stays=[first, elsewhere, second]).segments) == 3


def test_missing_coordinates_fall_back_to_the_same_address():
    first = stay_item("a", lat=None, lon=None, start=_t("09:00"), end=_t("10:00"), address="같은 주소", places=[])
    second = stay_item("b", lat=None, lon=None, start=_t("14:00"), end=_t("15:00"), address="같은 주소", places=[])

    assert _kinds(_skeleton(stays=[first, second])) == [(SegmentKind.STAY, 2)]


# --- 별도 방문 (#201) — 250m 넘게 떨어진 두 곳에 각각 30분 넘게 머물렀으면 따로 ------------------

_APART_681M = 0.00613  # 위도 차 약 681m — 운영 사례의 두 장소 사이 거리


def _visits_681m():
    """운영 사례의 모양: 첫 장소 체류 3건, 7분 18초 공백, 두 번째 장소 체류 4건(첫 기록 28분)."""

    there = _BASE_LAT + _APART_681M
    first = [
        _stay("a1", "09:00", "09:25"),
        _stay("a2", "09:25", "09:50", lat=_BASE_LAT + 0.0003),
        _stay("a3", "09:50", "10:10"),
    ]
    second = [
        _stay("b1", "10:17:18", "10:45:18", lat=there),
        _stay("b2", "10:45:18", "11:20", lat=there + 0.0002),
        _stay("b3", "11:20", "11:50", lat=there),
        _stay("b4", "11:50", "12:30", lat=there),
    ]
    return first, second


def _shape(skeleton):
    return [(s.kind, s.raw_ids, s.start, s.end) for s in skeleton.segments]


def test_two_long_stays_681m_apart_are_separate_visits_despite_a_7_minute_gap():
    """두 번째 장소의 첫 기록은 28분이지만 그곳에 이어 머문 기록까지 세면 30분을 넘는다.
    나눈 사이는 7분이라 빈 시간이 아니고, 1km 안이라 기록되지 않은 이동도 아니다."""

    first, second = _visits_681m()

    skeleton = _skeleton(stays=[*first, *second])

    assert _kinds(skeleton) == [(SegmentKind.STAY, 3), (SegmentKind.STAY, 4)]
    here, there = skeleton.segments
    assert here.raw_ids == [stay.raw_id for stay in first]
    assert there.raw_ids == [stay.raw_id for stay in second]
    assert here.end.time().isoformat() == "10:10:00"
    assert there.start.time().isoformat() == "10:17:18"
    assert [e for e in skeleton.empty_times if e.before and e.after] == []


def test_the_same_input_gives_the_same_visits():
    first, second = _visits_681m()
    stays = [*first, *second]

    once = _shape(_skeleton(stays=stays))

    assert _shape(_skeleton(stays=stays)) == once
    assert _shape(_skeleton(stays=list(reversed(stays)))) == once


@pytest.mark.parametrize(
    ("meters", "segments", "unrecorded"),
    [
        (250.0, [(SegmentKind.STAY, 2)], False),
        (math.nextafter(250.0, math.inf), [(SegmentKind.STAY, 1), (SegmentKind.STAY, 1)], False),
        (1000.0, [(SegmentKind.STAY, 1), (SegmentKind.STAY, 1)], False),
        (math.nextafter(1000.0, math.inf), [(SegmentKind.STAY, 1), (SegmentKind.STAY, 1)], True),
    ],
    ids=["정확히 250m", "250m 직후", "정확히 1km", "1km 직후"],
)
def test_distance_boundaries_between_two_long_stays(monkeypatch, meters, segments, unrecorded):
    """거리 함수를 고정해 경계값을 그대로 잰다. 250m 까지는 같은 장소, 1km 까지는 기록되지 않은 이동이 아니다."""

    monkeypatch.setattr(
        location_skeleton,
        "distance_between",
        lambda left, right: None if left is None or right is None else (0.0 if left == right else meters),
    )
    first = _stay("a", "09:00", "10:00")
    second = _stay("b", "10:05", "11:00", lat=_BASE_LAT + 0.01)

    skeleton = _skeleton(stays=[first, second])

    assert _kinds(skeleton) == segments
    between = [e.kind for e in skeleton.empty_times if e.before and e.after]
    assert between == ([EmptyTimeKind.UNRECORDED_MOVEMENT] if unrecorded else [])


@pytest.mark.parametrize("measured", ["앞", "뒤"])
@pytest.mark.parametrize(
    ("seconds", "separate"),
    [(30 * 60 - 1, False), (30 * 60, False), (30 * 60 + 1, True)],
    ids=["29분59초", "정확히 30분", "30분 1초"],
)
def test_30_minutes_on_either_side_is_where_visits_separate(measured, seconds, separate):
    """다른 쪽은 2시간 머문 곳이다. 재는 쪽이 30분을 넘어야 다른 방문이다."""

    day = datetime(2026, 6, 20, 9, 0)

    def clock(moment):
        return moment.strftime("%H:%M:%S")

    if measured == "앞":
        short_start = day
        long_start = day + timedelta(seconds=seconds, minutes=5)
    else:
        long_start = day - timedelta(hours=2, minutes=5)
        short_start = day
    long_end = long_start + timedelta(hours=2)
    long_stay = _stay("long", clock(long_start), clock(long_end))
    measured_stay = _stay(
        "measured",
        clock(short_start),
        clock(short_start + timedelta(seconds=seconds)),
        lat=_BASE_LAT + 0.005,
    )

    kinds = _kinds(_skeleton(stays=[long_stay, measured_stay]))

    assert kinds == ([(SegmentKind.STAY, 1)] * 2 if separate else [(SegmentKind.STAY, 2)])


@pytest.mark.parametrize(
    ("second", "separate"),
    [
        (
            [("b1", "09:05", "09:20"), ("b2", "09:40", "09:55")],  # 걸친 시간 50분, 기록된 시간 30분
            False,
        ),
        (
            [("b1", "09:05", "09:25"), ("b2", "09:15", "09:35")],  # 기록 합 40분, 겹침을 빼면 30분
            False,
        ),
        (
            [("b1", "09:05", "09:15"), ("b2", "09:15", "10:00")],  # 첫 기록만 10분, 이어 머문 시간 55분
            True,
        ),
    ],
    ids=["공백은 세지 않음", "겹친 시간은 한 번만", "첫 기록만 짧음"],
)
def test_stay_time_counts_only_recorded_time_at_that_place(second, separate):
    there = _BASE_LAT + 0.005
    long_stay = _stay("a", "07:00", "09:00")
    stays = [_stay(label, start, end, lat=there) for label, start, end in second]

    kinds = _kinds(_skeleton(stays=[long_stay, *stays]))

    assert kinds == ([(SegmentKind.STAY, 1), (SegmentKind.STAY, 2)] if separate else [(SegmentKind.STAY, 3)])


def test_a_short_stop_between_two_long_visits_does_not_join_them():
    """경유는 앞 방문에 남는다. 바로 앞(경유)만 견주면 셋이 다시 한 구간이 된다."""

    home = _stay("home", "07:00", "09:00")
    stop = _stay("stop", "09:05", "09:30", lat=_BASE_LAT + 0.005)  # 집에서 약 556m
    office = _stay("office", "09:35", "12:00", lat=_BASE_LAT + 0.008)  # 경유에서 약 333m, 집에서 약 890m

    skeleton = _skeleton(stays=[home, stop, office])

    assert [segment.raw_ids for segment in skeleton.segments] == [
        [home.raw_id, stop.raw_id],
        [office.raw_id],
    ]


def test_a_short_stop_and_back_to_the_same_place_is_one_visit():
    home = _stay("home-1", "07:00", "09:00")
    stop = _stay("stop", "09:05", "09:30", lat=_BASE_LAT + 0.005)
    back = _stay("home-2", "09:35", "12:00", lat=_BASE_LAT + 0.0005)

    assert _kinds(_skeleton(stays=[home, stop, back])) == [(SegmentKind.STAY, 3)]


def test_beyond_1km_a_short_side_still_means_another_place():
    """한쪽이 짧은 경유라도 1km 넘게 떨어졌으면 지금처럼 따로이고 기록되지 않은 이동이다."""

    first = _stay("a", "09:00", "11:00")
    second = _stay("b", "11:05", "11:30", lat=_BASE_LAT + 0.02)

    skeleton = _skeleton(stays=[first, second])

    assert _kinds(skeleton) == [(SegmentKind.STAY, 1), (SegmentKind.STAY, 1)]
    between = [e.kind for e in skeleton.empty_times if e.before and e.after]
    assert between == [EmptyTimeKind.UNRECORDED_MOVEMENT]


def test_a_movement_between_two_long_stays_681m_apart_stays_a_movement():
    first = _stay("a", "07:00", "09:00")
    movement = _move("m", "09:00", "09:10", to_lat=_BASE_LAT + _APART_681M)
    second = _stay("b", "09:10", "12:00", lat=_BASE_LAT + _APART_681M)

    assert _kinds(_skeleton(stays=[first, second], movements=[movement])) == [
        (SegmentKind.STAY, 1),
        (SegmentKind.MOVEMENT, 1),
        (SegmentKind.STAY, 1),
    ]


def test_without_coordinates_two_long_stays_stay_together_when_continuous():
    """거리를 모르면 30분 규칙으로 나누지 않는다 — 이름이 달라도 이어지면 지금처럼 한 구간이다."""

    first = stay_item(
        "a", lat=None, lon=None, start=_t("09:00"), end=_t("10:00"), address="주소 가", places=["공학관"]
    )
    second = stay_item(
        "b", lat=None, lon=None, start=_t("10:05"), end=_t("12:00"), address="주소 나", places=["도서관"]
    )

    assert _kinds(_skeleton(stays=[first, second])) == [(SegmentKind.STAY, 2)]


# --- 빈 시간 (L22·L23·L29) ---------------------------------------------------------


def test_L22_L23_gap_between_movements_records_both_ends_and_whether_they_match():
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    second = _move("2", "11:00", "11:30", from_lat=_BASE_LAT + 0.021, to_lat=_BASE_LAT + 0.05)

    skeleton = _skeleton(movements=[first, second])

    [gap] = [e for e in skeleton.empty_times if e.before and e.after]
    assert gap.kind is EmptyTimeKind.COLLECTION_GAP
    assert gap.same_place is True
    assert (gap.start.hour, gap.start.minute, gap.end.hour) == (9, 30, 11)
    assert skeleton.segments[0].end.minute == 30, "이동 시간을 공백까지 늘리지 않는다."


def test_L29_last_observation_and_trailing_gap():
    stay = _stay("a", "09:00", "21:00", places=["집 근처 아파트"])

    skeleton = _skeleton(stays=[stay])

    assert skeleton.last_observed_at.hour == 21
    assert skeleton.last_spot.places == ("집 근처 아파트",)
    trailing = skeleton.empty_times[-1]
    assert (trailing.start.hour, trailing.end.day) == (21, 21)


def test_L29_short_tail_is_not_an_empty_time():
    stay = stay_item("a", start=_t("00:00"), end="2026-06-20T23:30:00")

    assert _skeleton(stays=[stay]).empty_times == []


# --- 산책 (L24~L27) ----------------------------------------------------------------


def _walk(label, start, end, *, from_lat, to_lat, transports=("WALKING",), distance=800.0):
    return _move(label, start, end, from_lat=from_lat, to_lat=to_lat, transports=transports, distance=distance)


def test_L24_L27_round_trip_on_foot_for_30_minutes_or_more_is_a_walk():
    out = _walk("out", "19:00", "19:20", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.008)
    rest = _stay("bench", "19:20", "19:30", lat=_BASE_LAT + 0.008)
    back = _walk("back", "19:30", "19:50", from_lat=_BASE_LAT + 0.008, to_lat=_BASE_LAT + 0.0005)

    [walk] = _skeleton(stays=[rest], movements=[out, back]).segments

    assert walk.kind is SegmentKind.WALK
    assert (walk.start.minute, walk.end.minute) == (0, 50)
    assert rest.raw_id in walk.raw_ids


def test_L25_round_trip_on_foot_under_30_minutes_is_a_short_outing():
    out = _walk("out", "19:00", "19:10", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.003, distance=300.0)
    back = _walk("back", "19:10", "19:20", from_lat=_BASE_LAT + 0.003, to_lat=_BASE_LAT, distance=300.0)

    [outing] = _skeleton(movements=[out, back]).segments

    assert outing.kind is SegmentKind.MOVEMENT
    assert outing.round_trip is True


def test_L26_a_vehicle_leg_is_not_a_walk():
    out = _walk("out", "19:00", "19:20", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.008)
    back = _walk(
        "back", "19:20", "19:45", from_lat=_BASE_LAT + 0.008, to_lat=_BASE_LAT,
        transports=("IN_VEHICLE",),
    )

    [journey] = _skeleton(movements=[out, back]).segments

    assert journey.kind is SegmentKind.MOVEMENT
    assert journey.round_trip is False


def test_L26_mode_conflict_is_not_a_walk():
    out = _walk("out", "19:00", "19:20", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.008, distance=8000.0)
    back = _walk("back", "19:20", "19:40", from_lat=_BASE_LAT + 0.008, to_lat=_BASE_LAT)

    assert _skeleton(movements=[out, back]).segments[0].kind is SegmentKind.MOVEMENT


def test_L26_unknown_mode_is_not_a_walk():
    out = _walk("out", "19:00", "19:20", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.008, transports=(), distance=None)
    back = _walk("back", "19:20", "19:40", from_lat=_BASE_LAT + 0.008, to_lat=_BASE_LAT, transports=(), distance=None)
    # 거리도 라벨도 없는 이동은 끝점 거리로 속도를 잰다. 끝점이 같으면 거리가 0 에 가까워
    # 도보로 잡히므로, 여기서는 라벨만 지우고 출발지에 좌표가 없게 만든다.
    out.start.latitude = None
    back.end.latitude = None

    assert _skeleton(movements=[out, back]).segments[0].kind is SegmentKind.MOVEMENT


def test_L24_destination_elsewhere_is_not_a_walk():
    out = _walk("out", "19:00", "19:40", from_lat=_BASE_LAT, to_lat=_BASE_LAT + 0.01)

    assert _skeleton(movements=[out]).segments[0].kind is SegmentKind.MOVEMENT


# --- 장소 이름 정리 ----------------------------------------------------------------


def test_unit_variants_of_one_building_collapse_to_the_shorter_name():
    assert unique_place_names(
        ["동백호수공원 두산위브더제니스", "동백호수공원두산위브더제니스아파트 106동"]
    ) == ["동백호수공원 두산위브더제니스"]
    assert unique_place_names(
        ["동백호수공원두산위브더제니스아파트 106동", "동백호수공원 두산위브더제니스"]
    ) == ["동백호수공원 두산위브더제니스"]


def test_a_place_inside_a_building_is_kept_as_its_own_name():
    assert unique_place_names(["신라스테이 마포", "신라스테이 마포 카페", "근처"]) == [
        "신라스테이 마포",
        "신라스테이 마포 카페",
    ]
