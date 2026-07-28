"""이동 없이 이어진 체류 묶기 검증.

위치 수집이 끊겨 같은 장소의 STAY 가 조각으로 들어와도, 사이에 이동이 없으면 사람은
그 자리에 계속 있었던 것이다. 반대로 사이에 이동이나 수면이 있으면 이어진 체류가 아니다.
"""

from app.services.stay_merge import mergeable_stay_groups
from app.services.validator import resolve_timezone
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    movement_item,
    sleep_item,
    stay_item,
)

DAY = "2026-06-20"
TZ = resolve_timezone("Asia/Seoul")

HOME = "오산운암3단지 주공아파트"
HOME_ADDRESS = "경기도 오산시 운암로 90"


def _stay(item_id, raw_id, start, end, place=HOME, address=HOME_ADDRESS):
    return stay_item(
        item_id,
        raw_id=raw_id,
        start=f"{DAY}T{start}:00",
        end=f"{DAY}T{end}:00",
        place=place,
        address=address,
        places=[],
    )


def _groups(**overrides) -> list[frozenset[str]]:
    return mergeable_stay_groups(make_request(**overrides), TZ)


def _raw_group(*labels: str) -> frozenset[str]:
    return frozenset(fixture_raw_id(label) for label in labels)


# --- 병합 ---------------------------------------------------------------------


def test_same_place_stays_with_no_movement_between_are_merged():
    # 실제 사례: 집에서 나가지 않았는데 STAY 가 8분·9분짜리 조각으로 흩어졌다.
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:12", "11:20"),
            _stay(2, "stay-b", "14:26", "14:36"),
        ]
    )

    assert groups == [_raw_group("stay-a", "stay-b")]


def test_a_three_hour_gap_is_no_obstacle_when_there_was_no_movement():
    # 공백 길이는 기준이 아니다. 이동이 없었다는 사실이 기준이다.
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:12", "11:20"),
            _stay(2, "stay-b", "14:26", "14:36"),
            _stay(3, "stay-c", "17:00", "17:05"),
        ]
    )

    assert groups == [_raw_group("stay-a", "stay-b", "stay-c")]


def test_touching_stays_are_merged():
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:00", "12:00"),
            _stay(2, "stay-b", "12:00", "13:00"),
        ]
    )

    assert groups == [_raw_group("stay-a", "stay-b")]


def test_address_decides_when_neither_stay_has_a_place_name():
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:12", "11:20", place=None),
            _stay(2, "stay-b", "14:26", "14:36", place=None),
        ]
    )

    assert groups == [_raw_group("stay-a", "stay-b")]


# --- 병합하지 않음 -------------------------------------------------------------


def test_a_movement_in_the_gap_blocks_the_merge():
    # 나갔다가 돌아온 것이다. 이어진 체류가 아니다.
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:12", "11:20"),
            _stay(2, "stay-b", "14:26", "14:36"),
        ],
        movements=[
            movement_item(3, raw_id="move-1", start=f"{DAY}T12:00:00", end=f"{DAY}T12:30:00")
        ],
    )

    assert groups == []


def test_sleep_in_the_gap_blocks_the_merge():
    # 자고 일어난 것은 이어진 체류가 아니라 하루의 경계다.
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "00:35", "00:53"),
            _stay(2, "stay-b", "11:12", "11:20"),
        ],
        healths=[
            sleep_item(3, f"{DAY}T01:10:00", f"{DAY}T06:50:00", 340, raw_id="sleep-1")
        ],
    )

    assert groups == []


def test_a_nearby_place_is_not_the_same_place():
    # `...주공아파트` 와 `...주공아파트 인근` 은 다른 곳이다. 후자는 산책하러 나간 곳이다.
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:12", "11:20"),
            _stay(2, "stay-b", "14:26", "14:36", place=f"{HOME} 인근", address=f"{HOME_ADDRESS} 인근"),
        ]
    )

    assert groups == []


def test_a_named_place_is_not_merged_with_a_nameless_one():
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:12", "11:20"),
            _stay(2, "stay-b", "14:26", "14:36", place=None),
        ]
    )

    assert groups == []


def test_overlapping_stays_are_a_duplicate_record_not_a_broken_chain():
    # 시간이 겹치는 두 STAY 는 끊긴 수집이 아니다. 겹침 정리가 볼 문제다.
    groups = _groups(
        stays=[
            _stay(1, "stay-a", "11:00", "14:00"),
            _stay(2, "stay-b", "12:00", "13:00"),
        ]
    )

    assert groups == []


def test_a_single_stay_has_nothing_to_merge():
    assert _groups(stays=[_stay(1, "stay-a", "11:12", "11:20")]) == []
    assert _groups() == []


def test_the_chain_breaks_and_resumes_around_an_outing():
    # 집 → (이동) → 카페 → (이동) → 집. 앞뒤 집 체류는 같은 장소지만 이어지지 않았다.
    groups = _groups(
        stays=[
            _stay(1, "home-a", "09:00", "10:00"),
            _stay(2, "cafe", "10:30", "12:00", place="카페", address="다른 주소"),
            _stay(3, "home-b", "12:30", "13:00"),
            _stay(4, "home-c", "15:00", "16:00"),
        ],
        movements=[
            movement_item(5, raw_id="out", start=f"{DAY}T10:00:00", end=f"{DAY}T10:30:00"),
            movement_item(6, raw_id="back", start=f"{DAY}T12:00:00", end=f"{DAY}T12:30:00"),
        ],
    )

    # 귀가 후의 두 체류만 이어진다. 그 사이에는 이동이 없다.
    assert groups == [_raw_group("home-b", "home-c")]
