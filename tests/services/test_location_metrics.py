"""Location 파생 지표 계산 (#56 §4.4 필수 전처리).

프롬프트가 "평균 속도", "구간 공백" 을 근거로 판단하라고 요구하는데 입력 DTO 에는 그
필드가 없었다. 이 모듈이 기존 값만으로 계산해 채운다. 핵심은 **계산할 수 없으면 값을
만들지 않는 것** 이다 — 없는 근거를 있는 것처럼 실으면 LLM 이 그 위에 추론을 쌓는다.
"""

from app.services.location_metrics import build_location_metrics
from tests.fixtures.requests import make_request, movement_item, stay_item


def _movement(item_id, *, start, end, distance=None, transports=None, raw_id=None):
    return movement_item(
        item_id,
        start=start,
        end=end,
        distance=distance,
        transports=transports or [],
        raw_id=raw_id,
    )


def test_average_speed_is_computed_from_distance_and_duration():
    request = make_request(
        movements=[
            _movement(
                "m1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T10:00:00",
                distance=60_000,
            )
        ]
    )

    metric = build_location_metrics(request).movements[0]

    assert metric.duration_minutes == 60
    assert metric.average_speed_kmh == 60.0


def test_speed_is_absent_when_duration_is_unknown():
    """계산할 수 없으면 값을 만들지 않는다."""

    request = make_request(
        movements=[
            _movement(
                "m1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T09:00:00",
                distance=1_000,
            )
        ]
    )

    metric = build_location_metrics(request).movements[0]

    assert metric.average_speed_kmh is None
    assert "averageSpeedKmh" not in metric.as_prompt_dict()


def test_walking_label_conflicts_with_vehicle_speed():
    request = make_request(
        movements=[
            _movement(
                "m1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T10:00:00",
                distance=60_000,
                transports=["WALKING"],
            )
        ]
    )

    metric = build_location_metrics(request).movements[0]

    assert metric.transport_conflict is not None
    assert "WALKING" in metric.transport_conflict


def test_realistic_transport_label_has_no_conflict():
    request = make_request(
        movements=[
            _movement(
                "m1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T10:00:00",
                distance=4_000,
                transports=["WALKING"],
            )
        ]
    )

    assert build_location_metrics(request).movements[0].transport_conflict is None


def test_gap_between_movements_is_measured():
    request = make_request(
        movements=[
            _movement("m1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00"),
            _movement("m2", start="2026-06-20T13:00:00", end="2026-06-20T14:00:00"),
        ]
    )

    gaps = build_location_metrics(request).gaps

    assert len(gaps) == 1
    assert gaps[0].gap_minutes == 180
    assert gaps[0].has_stay_between is False


def test_gap_knows_when_a_stay_explains_it():
    request = make_request(
        movements=[
            _movement("m1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00"),
            _movement("m2", start="2026-06-20T13:00:00", end="2026-06-20T14:00:00"),
        ],
        stays=[
            stay_item("s1", start="2026-06-20T10:10:00", end="2026-06-20T12:50:00")
        ],
    )

    assert build_location_metrics(request).gaps[0].has_stay_between is True


def test_short_stays_are_flagged():
    request = make_request(
        stays=[
            stay_item("short", start="2026-06-20T09:00:00", end="2026-06-20T09:10:00"),
            stay_item("long", start="2026-06-20T10:00:00", end="2026-06-20T14:00:00"),
        ]
    )

    short = build_location_metrics(request).short_stay_raw_ids

    assert len(short) == 1


def test_coverage_gap_is_measured_from_last_observation():
    request = make_request(
        stays=[stay_item("s1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00")]
    )

    metrics = build_location_metrics(request)

    assert metrics.last_observed_at is not None
    assert metrics.last_observed_at.hour == 10
    # window 는 다음 날 00:00 까지라 14시간이 비어 있다.
    assert metrics.coverage_gap_minutes == 14 * 60


def test_short_coverage_gap_is_not_reported():
    """수집 주기 수준의 공백은 알리지 않는다. 매번 경고가 붙으면 신호가 죽는다."""

    request = make_request(
        stays=[stay_item("s1", start="2026-06-20T09:00:00", end="2026-06-20T23:40:00")]
    )

    assert build_location_metrics(request).coverage_gap_minutes is None


def test_region_change_is_reported_by_places():
    request = make_request(
        stays=[
            stay_item("s1", start="2026-06-20T08:00:00", end="2026-06-20T09:00:00", place="집"),
            stay_item("s2", start="2026-06-20T18:00:00", end="2026-06-20T19:00:00", place="서울드래곤시티"),
        ]
    )

    metrics = build_location_metrics(request)

    assert metrics.origin_place == "집"
    assert metrics.final_place == "서울드래곤시티"
    assert metrics.region_changed is True


def test_region_change_is_unknown_without_places():
    request = make_request(
        stays=[stay_item("s1", start="2026-06-20T08:00:00", end="2026-06-20T09:00:00", place=None, address=None)]
    )

    assert build_location_metrics(request).region_changed is None


def test_prompt_dict_omits_missing_values():
    """빈 값을 실어 보내면 LLM 이 의미를 부여한다."""

    payload = build_location_metrics(make_request()).as_prompt_dict()

    assert payload == {}
