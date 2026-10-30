"""위치 뼈대 (#167, v4 세트).

v3 Location Agent(LLM)가 프롬프트 규칙으로 하던 일을 코드가 한다. STAY·MOVEMENT 기록을
하루의 뼈대인 **구간**(체류·이동·산책)으로 묶고, 구간 사이의 **빈 시간**과 어떤 흐름에도
이어지지 않은 **단독 기록**을 따로 둔다. 같은 입력이면 같은 뼈대가 나온다.

뼈대는 관측 사실만 담는다. 제목·문장·eventType·확신도는 쓰지 않는다 — 그것은 Timeline v4 가
다른 단서와 함께 정한다. 집·학교·회사 같은 생활 장소 라벨도 붙이지 않는다.

## 묶는 규칙

규칙의 출처는 v3 Location 프롬프트(`location/prompts/v3/prompt.md`)이고, 줄마다 대응은 계획
(`2026-10-23-issue-167-…`)의 「Location 규칙 이식표」에 있다.

- **이어짐**: 앞 기록의 끝과 뒤 기록의 시작 사이가 45분 미만이다(`is_continuous`).
- **짧은 체류(20분 이하)**: 이어진 이동이 앞이나 뒤에 있으면 그 이동에 묶는다. 이동과 긴 체류
  사이에 낀 짧은 체류도 이동 쪽이다. 이동이 없으면 체류끼리의 규칙을 따른다.
- **이동**: 이어진 이동과 그 사이의 짧은 체류는 하나의 여정이다. 20분을 넘는 체류는 예외 없이
  여정을 끊는다(장소명이 없어도, 환승·같은 캠퍼스여도).
- **체류끼리**(사이에 이동 기록이 없을 때만, 바로 앞 체류와 잰다):
  - 250m 이내면 같은 장소다. 공백이 아무리 길어도 하나로 묶는다(수집은 끊길 수 있다).
  - 1km 넘게 떨어지면 언제나 다른 장소다. 그 사이는 **기록되지 않은 이동**이다.
  - 250m~1km 는 이어졌을 때만 묶는다(장소명이 달라도 하나 — 캠퍼스·단지).
  - 좌표가 없으면 주소나 장소명이 같을 때 같은 장소로 보고, 아니면 250m~1km 처럼 다룬다.
- **산책**: 한 여정의 출발지와 최종 도착지가 같고(같은 이름이거나 250m 이내), 전체 30분
  이상이며, 모든 이동이 도보이고 이동 방식 충돌이 없다. 30분 미만 도보 왕복은 왕복 표시를 단
  이동이다. 방식을 모르는 이동이 하나라도 있으면 산책이 아니다.
- **단독 기록**: 혼자 남은 20분 이하 체류, 앞뒤 어느 기록과도 이어지지 않은 20분 이하 이동.
  event 를 만들지 않고 근거를 보태기만 한다.

## 시간

구간의 시간은 묶은 기록의 첫 시작부터 마지막 끝까지이고 요청 window 로 자른다. 기록의 원래
시각은 구간 안 기록에 그대로 남는다. 원본 기록이 겹쳐 구간이 겹치면 뒤 구간의 시작을 앞 구간의
끝으로 맞춘다. 길이(20분 판정)는 자르기 전 원래 기록으로 잰다.

모든 STAY·MOVEMENT rawId 는 구간·단독 기록·배치하지 못한 기록 중 정확히 한 곳에 있다.
시각을 읽지 못했거나 window 밖에 통째로 있는 기록은 버리지 않고 배치하지 못한 기록으로 둔다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum

from app.schemas import GeoPlace, MovementItem, StayItem, TimelineDraftRequest
from app.services.location_metrics import (
    DIFFERENT_PLACE_MIN_METERS,
    SAME_PLACE_MAX_METERS,
    SHORT_STAY_MAX,
    WALK_MIN_DURATION,
    MovementMetric,
    MovementMode,
    build_location_metrics,
    distance_between,
    is_continuous,
)
from app.services.place_resolver import is_exact_address, unique_place_names
from app.services.place_text import normalize_place_text
from app.services.validator import parse_datetime, resolve_timezone


class SegmentKind(StrEnum):
    """뼈대 구간의 종류. 활동을 말하지 않는다 — 어디 있었는지와 움직였는지만 말한다."""

    STAY = "STAY"
    MOVEMENT = "MOVEMENT"
    WALK = "WALK"


class EmptyTimeKind(StrEnum):
    """구간 사이 빈 시간의 종류."""

    #: 위치 기록이 끊긴 시간. 사람이 사라진 것이 아니라 어딘가 있었던 것이다.
    COLLECTION_GAP = "COLLECTION_GAP"
    #: 이동 기록 없이 장소가 1km 넘게 바뀐 사이. 그 사이 어딘가로 움직였다.
    UNRECORDED_MOVEMENT = "UNRECORDED_MOVEMENT"


@dataclass(frozen=True)
class Spot:
    """한 지점: 장소명 후보, 주소, 좌표."""

    places: tuple[str, ...]
    address: str | None
    latitude: float | None
    longitude: float | None

    @property
    def point(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return self.latitude, self.longitude

    @property
    def label(self) -> str | None:
        return self.places[0] if self.places else self.address


@dataclass(frozen=True)
class LocationRecord:
    """STAY·MOVEMENT 기록 하나. 시각은 원래 값이다(window 로 자르지 않는다)."""

    raw_id: str
    kind: str
    start: datetime
    end: datetime
    #: STAY 의 지점.
    spot: Spot | None = None
    #: MOVEMENT 의 출발·도착 지점.
    origin: Spot | None = None
    destination: Spot | None = None
    metric: MovementMetric | None = None

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def is_stay(self) -> bool:
        return self.kind == "STAY"

    @property
    def is_short(self) -> bool:
        return self.duration <= SHORT_STAY_MAX


@dataclass(frozen=True)
class InternalGap:
    """같은 장소로 묶은 구간 안에서 위치 기록이 끊긴 시간(45분 이상)."""

    start: datetime
    end: datetime


@dataclass
class SkeletonSegment:
    """하루 뼈대의 구간 하나."""

    segment_id: str
    kind: SegmentKind
    start: datetime
    end: datetime
    records: list[LocationRecord]
    #: 대표 지점. 체류는 가장 오래 머문 기록의 지점이고, 이동·산책은 최종 도착지다.
    #: 주소·좌표와 이름이 같은 지점을 가리키게 하려고 하나로 둔다.
    spot: Spot | None = None
    #: 장소 후보. 대표 지점의 이름이 앞이고, 체류는 구간 안 다른 기록의 이름이 뒤에 붙는다.
    places: list[str] = field(default_factory=list)
    #: 이동·산책의 출발지와 최종 도착지. 경유지는 기록에만 있다.
    origin: Spot | None = None
    destination: Spot | None = None
    distance_meters: float | None = None
    #: 출발지로 돌아온 30분 미만 도보 이동(근거리 외출).
    round_trip: bool = False
    #: 출발지와 최종 도착지의 이름이 달라 보이는가. 행정구역 사전 없이 문자열만 본다.
    region_changed: bool | None = None
    internal_gaps: list[InternalGap] = field(default_factory=list)

    @property
    def raw_ids(self) -> list[str]:
        return [record.raw_id for record in self.records]

    @property
    def address(self) -> str | None:
        return self.spot.address if self.spot else None

    @property
    def latitude(self) -> float | None:
        return self.spot.latitude if self.spot else None

    @property
    def longitude(self) -> float | None:
        return self.spot.longitude if self.spot else None


@dataclass(frozen=True)
class EmptyTime:
    """구간 사이, 또는 첫 관측 전·마지막 관측 뒤의 빈 시간."""

    kind: EmptyTimeKind
    start: datetime
    end: datetime
    #: 빈 시간 직전·직후에 있던 지점. 하루의 처음과 끝에는 한쪽이 없다.
    before: Spot | None = None
    after: Spot | None = None
    #: 두 지점이 같은 장소인가(같은 이름이거나 250m 이내). 한쪽이 없으면 None.
    same_place: bool | None = None


@dataclass
class LocationSkeleton:
    """하루의 위치 뼈대."""

    segments: list[SkeletonSegment] = field(default_factory=list)
    empty_times: list[EmptyTime] = field(default_factory=list)
    #: 어떤 흐름에도 이어지지 않은 짧은 기록. event 를 만들지 않고 근거를 보탠다.
    isolated: list[LocationRecord] = field(default_factory=list)
    #: 시각을 읽지 못했거나 window 밖에 통째로 있는 기록.
    unplaced_raw_ids: list[str] = field(default_factory=list)
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    #: 마지막으로 확인된 지점.
    last_spot: Spot | None = None

    @property
    def has_location(self) -> bool:
        """하루에 위치 기록이 하나라도 배치됐는가. 없으면 다음 단계는 다른 단서로만 짠다."""

        return bool(self.segments or self.isolated)

    def record_spans(self) -> dict[str, tuple[datetime, datetime]]:
        """구간에 담긴 기록의 rawId → 그 기록이 구간 안에서 차지하는 시간.

        기록의 원래 시각을 구간의 시간(window 로 자르고 겹침을 정리한 값)으로 자른 것이다. 원본이
        겹친 이동(11~13시)은 앞 체류(~12시) 뒤인 12~13시가 된다. 확정 단계가 시간을 맞출 때 원래
        시각 대신 이 값을 써야 뼈대가 정리한 시간이 되돌아가지 않는다. 구간 안에 남는 시간이 없는
        기록은 구간의 시작 한 점이다. 단독 기록과 배치하지 못한 기록은 싣지 않는다.
        """

        spans: dict[str, tuple[datetime, datetime]] = {}
        for segment in self.segments:
            for record in segment.records:
                start = max(record.start, segment.start)
                end = min(record.end, segment.end)
                spans[record.raw_id] = (start, end) if start < end else (segment.start, segment.start)
        return spans

    def raw_ids(self) -> list[str]:
        """뼈대가 담은 모든 rawId. 입력의 STAY·MOVEMENT 가 정확히 한 번씩 나온다."""

        ids = [raw_id for segment in self.segments for raw_id in segment.raw_ids]
        ids.extend(record.raw_id for record in self.isolated)
        ids.extend(self.unplaced_raw_ids)
        return ids


def build_location_skeleton(request: TimelineDraftRequest) -> LocationSkeleton:
    """요청의 STAY·MOVEMENT 로 하루의 위치 뼈대를 만든다. 위치 기록이 없어도 실패하지 않는다."""

    tz = resolve_timezone(request.timezone)
    window_start, window_end = _window(request, tz)
    metrics = {metric.raw_id: metric for metric in build_location_metrics(request).movements}
    records, unplaced = _records(request, tz, window_start, window_end, metrics)
    records.sort(key=lambda record: (record.start, record.end))

    skeleton = LocationSkeleton(unplaced_raw_ids=unplaced)
    if not records:
        return skeleton

    flows, far_splits = _flows(records)
    segments: list[SkeletonSegment] = []
    placed: list[tuple[SkeletonSegment | None, list[LocationRecord]]] = []
    for flow in flows:
        if _is_isolated(flow, records):
            skeleton.isolated.extend(flow)
            placed.append((None, flow))
            continue
        segment = _segment(flow, len(segments) + 1, window_start, window_end)
        segments.append(segment)
        placed.append((segment, flow))

    _remove_overlaps(segments)
    skeleton.segments = segments
    skeleton.empty_times = _empty_times(placed, far_splits, window_start, window_end)

    skeleton.first_observed_at = _clip(min(r.start for r in records), window_start, window_end)
    skeleton.last_observed_at = _clip(max(r.end for r in records), window_start, window_end)
    if segments:
        skeleton.last_spot = _exit_spot(segments[-1])
    return skeleton


# --- 기록 ----------------------------------------------------------------------


def _window(
    request: TimelineDraftRequest, tz: tzinfo
) -> tuple[datetime | None, datetime | None]:
    if request.window is None:
        return None, None
    return parse_datetime(request.window.start, tz), parse_datetime(request.window.end, tz)


def _records(
    request: TimelineDraftRequest,
    tz: tzinfo,
    window_start: datetime | None,
    window_end: datetime | None,
    metrics: dict[str, MovementMetric],
) -> tuple[list[LocationRecord], list[str]]:
    records: list[LocationRecord] = []
    unplaced: list[str] = []
    items: list[StayItem | MovementItem] = [*request.stays, *request.movements]
    for item in items:
        raw_id = str(item.raw_id)
        start = parse_datetime(item.start_at, tz)
        end = parse_datetime(item.end_at, tz) if item.end_at else start
        if start is None:
            unplaced.append(raw_id)
            continue
        if end is None or end < start:
            end = start
        if _outside(start, end, window_start, window_end):
            unplaced.append(raw_id)
            continue
        if isinstance(item, StayItem):
            records.append(
                LocationRecord(raw_id, "STAY", start, end, spot=_stay_spot(item))
            )
        else:
            records.append(
                LocationRecord(
                    raw_id,
                    "MOVEMENT",
                    start,
                    end,
                    origin=_geo_spot(item.start),
                    destination=_geo_spot(item.end),
                    metric=metrics.get(raw_id),
                )
            )
    return records, unplaced


def _outside(
    start: datetime, end: datetime, window_start: datetime | None, window_end: datetime | None
) -> bool:
    if window_start is not None and end < window_start:
        return True
    if window_end is not None and start >= window_end:
        return True
    return False


def _stay_spot(stay: StayItem) -> Spot:
    return Spot(
        places=tuple(unique_place_names([stay.place, *stay.places])),
        address=_exact(stay.address),
        latitude=stay.latitude,
        longitude=stay.longitude,
    )


def _geo_spot(place: GeoPlace | None) -> Spot | None:
    if place is None:
        return None
    return Spot(
        places=tuple(unique_place_names([place.place, *place.places])),
        address=_exact(place.address),
        latitude=place.latitude,
        longitude=place.longitude,
    )


def _exact(address: str | None) -> str | None:
    return address.strip() if is_exact_address(address) else None


# --- 흐름으로 묶기 ---------------------------------------------------------------


def _flows(records: list[LocationRecord]) -> tuple[list[list[LocationRecord]], set[int]]:
    """기록을 시간순 흐름으로 묶는다. 흐름 하나가 구간 하나나 단독 기록이 된다.

    두 번째 값은 1km 넘게 떨어져 갈린 체류 흐름의 자리다(앞 흐름의 번호). 그 사이는 빈 시간
    중에서도 기록되지 않은 이동으로 적는다.
    """

    to_movement = _short_stays_on_journeys(records)
    flows: list[list[LocationRecord]] = []
    far_splits: set[int] = set()
    current: list[LocationRecord] = []
    current_is_journey = False

    def close() -> None:
        nonlocal current
        if current:
            flows.append(current)
        current = []

    for index, record in enumerate(records):
        journey_part = not record.is_stay or index in to_movement
        if not current:
            current, current_is_journey = [record], journey_part
            continue
        reached = max(r.end for r in current)
        if journey_part and current_is_journey and is_continuous(record.start - reached):
            current.append(record)
            continue
        if not journey_part and not current_is_journey:
            decision = _stay_decision(current[-1], record, reached)
            if decision == "merge":
                current.append(record)
                continue
            close()
            if decision == "far":
                far_splits.add(len(flows) - 1)
            current, current_is_journey = [record], journey_part
            continue
        close()
        current, current_is_journey = [record], journey_part
    close()
    return flows, far_splits


def _short_stays_on_journeys(records: list[LocationRecord]) -> set[int]:
    """이어진 이동에 묶이는 짧은 체류의 자리.

    이동에 바로 이어진 짧은 체류와, 그 짧은 체류에 다시 이어진 짧은 체류까지 이동 쪽이다.
    그래서 앞뒤 두 방향으로 번진다.
    """

    attached: set[int] = set()

    def spread(order: range) -> None:
        for index in order:
            record = records[index]
            if not record.is_stay or not record.is_short or index in attached:
                continue
            for neighbor in (index - 1, index + 1):
                if not 0 <= neighbor < len(records):
                    continue
                other = records[neighbor]
                if other.is_stay and neighbor not in attached:
                    continue
                gap = (
                    record.start - other.end if neighbor < index else other.start - record.end
                )
                if is_continuous(gap):
                    attached.add(index)
                    break

    spread(range(len(records)))
    spread(range(len(records) - 1, -1, -1))
    return attached


def _stay_decision(previous: LocationRecord, record: LocationRecord, reached: datetime) -> str:
    """사이에 이동 기록이 없는 두 체류를 묶을지. `merge`·`split`·`far` 중 하나다.

    거리는 바로 앞 체류와 재고, 공백은 묶음이 닿은 마지막 시각부터 잰다.
    """

    distance = _spot_distance(previous.spot, record.spot)
    if distance is not None:
        if distance <= SAME_PLACE_MAX_METERS:
            return "merge"
        if distance > DIFFERENT_PLACE_MIN_METERS:
            return "far"
    elif _same_name(previous.spot, record.spot):
        return "merge"
    return "merge" if is_continuous(record.start - reached) else "split"


def _is_isolated(flow: list[LocationRecord], records: list[LocationRecord]) -> bool:
    """어떤 흐름에도 이어지지 않은 짧은 기록인가."""

    if len(flow) != 1:
        return False
    record = flow[0]
    if not record.is_short:
        return False
    if record.is_stay:
        # 이동에 묶이지 않았고 같은 장소 체류와도 묶이지 않은 짧은 체류다.
        return True
    index = records.index(record)
    for neighbor in (index - 1, index + 1):
        if not 0 <= neighbor < len(records):
            continue
        other = records[neighbor]
        gap = record.start - other.end if neighbor < index else other.start - record.end
        if is_continuous(gap):
            return False
    return True


# --- 구간 ----------------------------------------------------------------------


def _segment(
    flow: list[LocationRecord],
    number: int,
    window_start: datetime | None,
    window_end: datetime | None,
) -> SkeletonSegment:
    start = _clip(min(record.start for record in flow), window_start, window_end)
    end = _clip(max(record.end for record in flow), window_start, window_end)
    segment_id = f"S{number}"
    if all(record.is_stay for record in flow):
        return _stay_segment(segment_id, flow, start, end)
    return _journey_segment(segment_id, flow, start, end)


def _stay_segment(
    segment_id: str, flow: list[LocationRecord], start: datetime, end: datetime
) -> SkeletonSegment:
    by_duration = sorted(flow, key=lambda record: record.duration, reverse=True)
    main = by_duration[0].spot
    names = unique_place_names(
        name for record in by_duration if record.spot for name in record.spot.places
    )
    return SkeletonSegment(
        segment_id=segment_id,
        kind=SegmentKind.STAY,
        start=start,
        end=end,
        records=list(flow),
        spot=main,
        places=names,
        internal_gaps=_internal_gaps(flow),
    )


def _journey_segment(
    segment_id: str, flow: list[LocationRecord], start: datetime, end: datetime
) -> SkeletonSegment:
    movements = [record for record in flow if not record.is_stay]
    origin = movements[0].origin
    destination = movements[-1].destination
    distances = [
        record.metric.distance_meters
        for record in movements
        if record.metric and record.metric.distance_meters is not None
    ]
    all_walk = all(
        record.metric is not None
        and record.metric.mode is MovementMode.WALK
        and record.metric.mode_conflict is None
        for record in movements
    )
    back_home = _same_place(origin, destination)
    kind = SegmentKind.MOVEMENT
    round_trip = False
    if back_home and all_walk:
        if end - start >= WALK_MIN_DURATION:
            kind = SegmentKind.WALK
        else:
            round_trip = True
    return SkeletonSegment(
        segment_id=segment_id,
        kind=kind,
        start=start,
        end=end,
        records=list(flow),
        spot=destination,
        places=list(destination.places) if destination else [],
        origin=origin,
        destination=destination,
        distance_meters=sum(distances) if distances else None,
        round_trip=round_trip,
        region_changed=_region_changed(origin, destination),
    )


def _internal_gaps(flow: list[LocationRecord]) -> list[InternalGap]:
    gaps: list[InternalGap] = []
    reached = flow[0].end
    for record in flow[1:]:
        if not is_continuous(record.start - reached):
            gaps.append(InternalGap(start=reached, end=record.start))
        reached = max(reached, record.end)
    return gaps


def _remove_overlaps(segments: list[SkeletonSegment]) -> None:
    """원본 기록이 겹쳐 구간이 겹치면 뒤 구간의 시작을 앞 구간의 끝으로 맞춘다."""

    for previous, segment in zip(segments, segments[1:]):
        if segment.start < previous.end:
            segment.start = min(previous.end, segment.end)


# --- 빈 시간 --------------------------------------------------------------------


def _empty_times(
    placed: list[tuple[SkeletonSegment | None, list[LocationRecord]]],
    far_splits: set[int],
    window_start: datetime | None,
    window_end: datetime | None,
) -> list[EmptyTime]:
    """구간 사이의 빈 시간. 단독 기록은 하루를 덮지 않는다 — 빈 시간 안에 놓일 수 있다."""

    empty: list[EmptyTime] = []
    segments = [(index, segment) for index, (segment, _) in enumerate(placed) if segment]
    if not segments:
        return empty

    first = segments[0][1]
    if window_start is not None and not is_continuous(first.start - window_start):
        empty.append(
            EmptyTime(
                EmptyTimeKind.COLLECTION_GAP, window_start, first.start, after=_entry_spot(first)
            )
        )

    for (left_index, left), (right_index, right) in zip(segments, segments[1:]):
        before, after = _exit_spot(left), _entry_spot(right)
        # 사이에 단독 기록 흐름이 끼었어도, 그 사이 어디서든 1km 넘게 갈렸으면 기록되지 않은 이동이다.
        far = any(index in far_splits for index in range(left_index, right_index))
        gap_start, gap_end = left.end, max(left.end, right.start)
        if far:
            kind = EmptyTimeKind.UNRECORDED_MOVEMENT
        elif not is_continuous(gap_end - gap_start):
            kind = EmptyTimeKind.COLLECTION_GAP
        else:
            continue
        empty.append(
            EmptyTime(
                kind,
                gap_start,
                gap_end,
                before=before,
                after=after,
                same_place=_same_place(before, after),
            )
        )

    last = segments[-1][1]
    if window_end is not None and not is_continuous(window_end - last.end):
        empty.append(
            EmptyTime(EmptyTimeKind.COLLECTION_GAP, last.end, window_end, before=_exit_spot(last))
        )
    return empty


def _entry_spot(segment: SkeletonSegment) -> Spot | None:
    return segment.spot if segment.kind is SegmentKind.STAY else segment.origin


def _exit_spot(segment: SkeletonSegment) -> Spot | None:
    return segment.spot


# --- 지점 비교 ------------------------------------------------------------------


def _spot_distance(left: Spot | None, right: Spot | None) -> float | None:
    if left is None or right is None:
        return None
    return distance_between(left.point, right.point)


def _same_name(left: Spot | None, right: Spot | None) -> bool:
    if left is None or right is None:
        return False
    left_names = {normalize_place_text(name) for name in left.places}
    if left_names & {normalize_place_text(name) for name in right.places}:
        return True
    return bool(
        left.address
        and right.address
        and normalize_place_text(left.address) == normalize_place_text(right.address)
    )


def _same_place(left: Spot | None, right: Spot | None) -> bool | None:
    """두 지점이 같은 장소인가. 한쪽이 없으면 모른다."""

    if left is None or right is None:
        return None
    distance = _spot_distance(left, right)
    if distance is not None and distance <= SAME_PLACE_MAX_METERS:
        return True
    return _same_name(left, right)


def _region_changed(origin: Spot | None, destination: Spot | None) -> bool | None:
    left = origin.label if origin else None
    right = destination.label if destination else None
    if not left or not right:
        return None
    return normalize_place_text(left) != normalize_place_text(right)


def _clip(value: datetime, window_start: datetime | None, window_end: datetime | None) -> datetime:
    if window_start is not None and value < window_start:
        return window_start
    if window_end is not None and value > window_end:
        return window_end
    return value
