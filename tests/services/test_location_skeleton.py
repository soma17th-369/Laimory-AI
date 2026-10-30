"""위치 뼈대 검증 (#167).

v3 Location 프롬프트의 규칙을 코드로 옮긴 것이므로, 계획의 「Location 규칙 이식표」 줄마다
테스트를 둔다(테스트 이름 앞의 `L번호`). 거리 기준(250m·1km)은 사용자 결정이다(처음 300m, 2026-10-30 에 250m).

좌표는 위도 0.001 이 약 111m 다. 0.002 는 같은 장소(250m 이내), 0.005 는 250m~1km,
0.02 는 다른 장소(1km 초과)다.
"""

from app.schemas import GeoPlace, MovementItem
from app.services.location_skeleton import (
    EmptyTimeKind,
    SegmentKind,
    build_location_skeleton,
)
from app.services.place_resolver import unique_place_names
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

_BASE_LAT = 37.5
_BASE_LON = 127.0


def _t(hhmm: str) -> str:
    return f"2026-06-20T{hhmm}:00"


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


def test_L5_exactly_45_minutes_apart_is_not_continuous():
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    second = _move("2", "10:15", "10:45", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04)

    assert _kinds(_skeleton(movements=[first, second])) == [
        (SegmentKind.MOVEMENT, 1),
        (SegmentKind.MOVEMENT, 1),
    ]


def test_L5_44_minutes_apart_is_one_journey():
    first = _move("1", "09:00", "09:30", to_lat=_BASE_LAT + 0.02)
    second = _move("2", "10:14", "10:45", from_lat=_BASE_LAT + 0.02, to_lat=_BASE_LAT + 0.04)

    assert _kinds(_skeleton(movements=[first, second])) == [(SegmentKind.MOVEMENT, 2)]


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
    """캠퍼스·단지 안의 여러 지점. 대표 지점은 가장 오래 머문 체류다."""

    building = _stay("building", "09:00", "09:40", places=["공학관"])
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
