"""Location raw 파생 지표 계산 (#56 §4.4 필수 전처리).

Location Agent 프롬프트는 "평균 속도", "계산된 속도", "연속 MOVEMENT 사이의 시간 공백"
같은 값을 근거로 판단하라고 요구한다. 그런데 입력 DTO 에는 그런 필드가 없다 —
`MovementItem` 이 주는 것은 `distanceMeters`, `durationText`, `transports` 뿐이다.
LLM 이 구간마다 거리÷시간을 암산해야 했고, 그건 조용히 틀린다.

이 모듈은 **기존 DTO 값만으로** 그 파생값을 계산해 프롬프트에 실어 준다. 입력 계약
(`StayItem`/`MovementItem`)은 바꾸지 않는다. 계산이 불가능한 항목(좌표 없음, 시각 파싱
실패 등)은 값을 지어내지 않고 ``None`` 으로 남긴다 — 없는 근거를 있는 것처럼 보이게 하면
LLM 이 그 위에 추론을 쌓는다.

이동수단 현실성 판정은 "그 속도로 그 이동수단이 가능한가" 만 본다. 라벨을 고쳐 주지
않는다. 센서 라벨이 틀렸을 수 있다는 사실만 알려 주고, 최종 표현은 Agent 가 정한다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from math import asin, cos, radians, sin, sqrt
from typing import Any

from app.schemas import MovementItem, StayItem, TimelineDraftRequest
from app.services.place_text import normalize_place_text
from app.services.validator import parse_datetime, resolve_timezone

#: 이 시간 이하로 머문 중간 STAY 는 실제 방문보다 이동 중 위치 분절일 수 있다.
#: 요구사항 §12 가 20분을 기준으로 못 박았다.
SHORT_STAY_MAX = timedelta(minutes=20)

#: 이 시간 이상 위치 기록이 없으면 수집이 끊긴 구간으로 본다.
COVERAGE_GAP_MIN = timedelta(minutes=45)

#: 이동수단별 현실적인 평균 속도 상한(km/h). 신호 대기·환승을 포함한 구간 평균이라
#: 순간 최고 속도가 아니라 넉넉히 잡는다. 넘으면 라벨을 의심한다.
_TRANSPORT_MAX_KMH: dict[str, float] = {
    "WALKING": 8.0,
    "ON_FOOT": 8.0,
    "RUNNING": 20.0,
    "ON_BICYCLE": 35.0,
    "CYCLING": 35.0,
    "IN_VEHICLE": 160.0,
}

#: 이동수단별 현실적인 평균 속도 하한(km/h). 이보다 느리면 그 수단으로 보기 어렵다.
_TRANSPORT_MIN_KMH: dict[str, float] = {
    "IN_VEHICLE": 3.0,
}

_EARTH_RADIUS_M = 6_371_000.0


@dataclass
class MovementMetric:
    """MOVEMENT 한 건의 파생값."""

    raw_id: str
    start: datetime | None
    end: datetime | None
    duration_minutes: float | None
    distance_meters: float | None
    average_speed_kmh: float | None
    transports: list[str] = field(default_factory=list)
    #: 라벨과 평균 속도가 어긋나면 그 이유. 어긋나지 않으면 None.
    transport_conflict: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "rawId": self.raw_id,
                "durationMinutes": _round(self.duration_minutes, 1),
                "distanceMeters": _round(self.distance_meters, 0),
                "averageSpeedKmh": _round(self.average_speed_kmh, 1),
                "transports": self.transports or None,
                "transportConflict": self.transport_conflict,
            }
        )


@dataclass
class SegmentGap:
    """연속한 두 이동 사이의 공백."""

    after_raw_id: str
    before_raw_id: str
    gap_minutes: float
    #: 앞 이동의 도착지와 뒤 이동의 출발지가 얼마나 떨어져 있는가.
    endpoint_distance_meters: float | None
    #: 그 사이를 설명하는 STAY 기록이 있는가.
    has_stay_between: bool

    def as_prompt_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "afterRawId": self.after_raw_id,
                "beforeRawId": self.before_raw_id,
                "gapMinutes": _round(self.gap_minutes, 1),
                "endpointDistanceMeters": _round(self.endpoint_distance_meters, 0),
                "hasStayBetween": self.has_stay_between,
            }
        )


@dataclass
class LocationMetrics:
    """하루치 Location raw 에서 뽑은 파생 지표 묶음."""

    movements: list[MovementMetric] = field(default_factory=list)
    gaps: list[SegmentGap] = field(default_factory=list)
    short_stay_raw_ids: list[str] = field(default_factory=list)
    last_observed_at: datetime | None = None
    coverage_gap_minutes: float | None = None
    origin_place: str | None = None
    final_place: str | None = None
    region_changed: bool | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        """프롬프트에 실을 형태. 값이 없는 항목은 통째로 뺀다."""

        return _compact(
            {
                "movements": [m.as_prompt_dict() for m in self.movements] or None,
                "movementGaps": [g.as_prompt_dict() for g in self.gaps] or None,
                "shortStayRawIds": self.short_stay_raw_ids or None,
                "lastObservedAt": (
                    self.last_observed_at.isoformat() if self.last_observed_at else None
                ),
                "coverageGapMinutes": _round(self.coverage_gap_minutes, 1),
                "originPlace": self.origin_place,
                "finalPlace": self.final_place,
                "regionChanged": self.region_changed,
            }
        )


def build_location_metrics(request: TimelineDraftRequest) -> LocationMetrics:
    """요청의 STAY·MOVEMENT 에서 파생 지표를 계산한다.

    시각 파싱에 실패한 항목은 시간 기반 계산에서 빠지되, 목록에서 사라지지는 않는다.
    """

    tz = resolve_timezone(request.timezone)
    stays = _sorted_stays(request.stays, tz)
    movements = _sorted_movements(request.movements, tz)

    metrics = LocationMetrics(
        movements=[_movement_metric(item, start, end) for item, start, end in movements],
        gaps=_segment_gaps(movements, stays),
        short_stay_raw_ids=[
            str(item.raw_id)
            for item, start, end in stays
            if start is not None and end is not None and end - start <= SHORT_STAY_MAX
        ],
    )

    metrics.last_observed_at = _last_observed_at(stays, movements)
    metrics.coverage_gap_minutes = _coverage_gap_minutes(
        metrics.last_observed_at, request, tz
    )
    metrics.origin_place, metrics.final_place = _origin_and_final_place(stays, movements)
    metrics.region_changed = _region_changed(metrics.origin_place, metrics.final_place)
    return metrics


# --- 정렬 ---------------------------------------------------------------------


def _sorted_stays(
    items: list[StayItem], tz: tzinfo
) -> list[tuple[StayItem, datetime | None, datetime | None]]:
    rows = [
        (item, parse_datetime(item.start_at, tz), _end_of(item, tz)) for item in items
    ]
    return sorted(rows, key=lambda row: (row[1] is None, row[1]))


def _sorted_movements(
    items: list[MovementItem], tz: tzinfo
) -> list[tuple[MovementItem, datetime | None, datetime | None]]:
    rows = [
        (item, parse_datetime(item.start_at, tz), _end_of(item, tz)) for item in items
    ]
    return sorted(rows, key=lambda row: (row[1] is None, row[1]))


def _end_of(item: StayItem | MovementItem, tz: tzinfo) -> datetime | None:
    """`endAt` 이 없으면 시작 시각을 끝으로 본다(순간 기록)."""

    if item.end_at:
        return parse_datetime(item.end_at, tz)
    return parse_datetime(item.start_at, tz)


# --- 이동별 파생값 -------------------------------------------------------------


def _movement_metric(
    item: MovementItem, start: datetime | None, end: datetime | None
) -> MovementMetric:
    duration_minutes = None
    if start is not None and end is not None and end > start:
        duration_minutes = (end - start).total_seconds() / 60.0

    distance = item.distance_meters
    if distance is None:
        distance = _endpoint_distance(item)

    speed_kmh = None
    if duration_minutes and distance is not None:
        speed_kmh = (distance / 1000.0) / (duration_minutes / 60.0)

    transports = [str(value).upper() for value in item.transports]
    return MovementMetric(
        raw_id=str(item.raw_id),
        start=start,
        end=end,
        duration_minutes=duration_minutes,
        distance_meters=distance,
        average_speed_kmh=speed_kmh,
        transports=transports,
        transport_conflict=_transport_conflict(transports, speed_kmh),
    )


def _transport_conflict(transports: list[str], speed_kmh: float | None) -> str | None:
    """센서 라벨이 계산된 평균 속도로 설명되는가.

    라벨을 고치지 않는다. 어긋난다는 사실만 돌려주고 판단은 Agent 에 맡긴다.
    """

    if speed_kmh is None:
        return None

    for label in transports:
        upper = _TRANSPORT_MAX_KMH.get(label)
        if upper is not None and speed_kmh > upper:
            return (
                f"{label} 라벨이지만 평균 {speed_kmh:.1f}km/h 로 "
                f"{upper:.0f}km/h 상한을 넘습니다."
            )
        lower = _TRANSPORT_MIN_KMH.get(label)
        if lower is not None and speed_kmh < lower:
            return (
                f"{label} 라벨이지만 평균 {speed_kmh:.1f}km/h 로 "
                f"{lower:.0f}km/h 하한에 못 미칩니다."
            )
    return None


# --- 이동 사이 공백 ------------------------------------------------------------


def _segment_gaps(
    movements: list[tuple[MovementItem, datetime | None, datetime | None]],
    stays: list[tuple[StayItem, datetime | None, datetime | None]],
) -> list[SegmentGap]:
    """연속한 두 이동 사이의 시간 공백과 끝점 거리를 계산한다."""

    gaps: list[SegmentGap] = []
    for (prev, _, prev_end), (nxt, next_start, _) in zip(movements, movements[1:]):
        if prev_end is None or next_start is None or next_start <= prev_end:
            continue
        gaps.append(
            SegmentGap(
                after_raw_id=str(prev.raw_id),
                before_raw_id=str(nxt.raw_id),
                gap_minutes=(next_start - prev_end).total_seconds() / 60.0,
                endpoint_distance_meters=_distance_between(
                    _point_of(prev.end), _point_of(nxt.start)
                ),
                has_stay_between=_has_stay_between(stays, prev_end, next_start),
            )
        )
    return gaps


def _has_stay_between(
    stays: list[tuple[StayItem, datetime | None, datetime | None]],
    start: datetime,
    end: datetime,
) -> bool:
    return any(
        s_start is not None and s_end is not None and s_start < end and s_end > start
        for _, s_start, s_end in stays
    )


# --- 수집 공백 -----------------------------------------------------------------


def _last_observed_at(
    stays: list[tuple[StayItem, datetime | None, datetime | None]],
    movements: list[tuple[MovementItem, datetime | None, datetime | None]],
) -> datetime | None:
    ends = [end for _, _, end in [*stays, *movements] if end is not None]
    return max(ends) if ends else None


def _coverage_gap_minutes(
    last_observed_at: datetime | None, request: TimelineDraftRequest, tz: tzinfo
) -> float | None:
    """마지막 관측 이후 window 끝까지 비어 있는 시간.

    공백이 짧으면(수집 주기 수준) 의미가 없으므로 ``COVERAGE_GAP_MIN`` 미만은 알리지 않는다.
    """

    if last_observed_at is None or request.window is None:
        return None
    window_end = parse_datetime(request.window.end, tz)
    if window_end is None or window_end <= last_observed_at:
        return None
    gap = window_end - last_observed_at
    if gap < COVERAGE_GAP_MIN:
        return None
    return gap.total_seconds() / 60.0


# --- 출발지 / 최종 도착지 -------------------------------------------------------


def _origin_and_final_place(
    stays: list[tuple[StayItem, datetime | None, datetime | None]],
    movements: list[tuple[MovementItem, datetime | None, datetime | None]],
) -> tuple[str | None, str | None]:
    """하루의 첫 지점과 마지막 지점의 장소 라벨."""

    labeled: list[tuple[datetime, str]] = []
    for item, start, _ in stays:
        label = item.place or item.address
        if start is not None and label:
            labeled.append((start, label))
    for item, start, end in movements:
        if start is not None and (origin := _label_of(item.start)):
            labeled.append((start, origin))
        if end is not None and (target := _label_of(item.end)):
            labeled.append((end, target))

    if not labeled:
        return None, None
    labeled.sort(key=lambda row: row[0])
    return labeled[0][1], labeled[-1][1]


def _region_changed(origin: str | None, final: str | None) -> bool | None:
    """출발지와 최종 도착지가 서로 다른 장소를 가리키는가.

    행정구역 사전 없이 문자열만 보므로 "지역이 바뀌었다" 를 단정하지 못한다.
    같은 곳으로 보이는지 아닌지까지만 알려 주고 해석은 Agent 에 맡긴다.
    """

    if not origin or not final:
        return None
    return normalize_place_text(origin) != normalize_place_text(final)


# --- 좌표 ----------------------------------------------------------------------


def _point_of(place: Any) -> tuple[float, float] | None:
    if place is None:
        return None
    lat = getattr(place, "latitude", None)
    lon = getattr(place, "longitude", None)
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def _label_of(place: Any) -> str | None:
    if place is None:
        return None
    return getattr(place, "place", None) or getattr(place, "address", None)


def _endpoint_distance(item: MovementItem) -> float | None:
    return _distance_between(_point_of(item.start), _point_of(item.end))


def _distance_between(
    left: tuple[float, float] | None, right: tuple[float, float] | None
) -> float | None:
    """두 좌표 사이의 대권 거리(m). 한쪽이라도 좌표가 없으면 계산하지 않는다."""

    if left is None or right is None:
        return None
    lat1, lon1 = radians(left[0]), radians(left[1])
    lat2, lon2 = radians(right[0]), radians(right[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(h))


# --- 출력 정리 -----------------------------------------------------------------


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits) if digits else round(value)


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """값이 없는 키를 지운다. 빈 값을 실어 보내면 LLM 이 의미를 부여한다."""

    return {key: value for key, value in payload.items() if value is not None}
