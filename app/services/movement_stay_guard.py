"""이동 사이에 낀 장시간 체류 검사 (#119).

Location 전처리는 20분 이하로 머문 STAY 를 `shortStayRawIds` 로 계산해 프롬프트에 싣는다.
짧은 체류만 이동 중 위치 분절로 볼 수 있다는 뜻이다. 그런데 그 값은 참고 정보일 뿐이라,
Agent 가 기준을 어겨 **몇 시간짜리 체류를 앞뒤 이동과 하나로 합쳐도** 잡는 단계가 없었다.
집에서 나가 회사에 8시간 있다가 돌아온 하루가 `회사에 다녀옴` 이동 하나가 된다.

이 모듈은 그 구조를 입력과 대조해 찾는다. 하나의 candidate·event 가 근거로 댄 rawId 안에

    MOVEMENT → 20분을 넘는 STAY → MOVEMENT

가 함께 있으면 위반이다. 기준은 `location_metrics.SHORT_STAY_MAX` 하나다 — 프롬프트에
싣는 값과 검사하는 값이 다르면 같은 체류를 두고 Agent 와 코드가 서로 다른 말을 한다.

**20분을 넘으면 예외가 없다.** 역·터미널·공항에서의 환승·대기도 나눈다.

**찾기만 하고 나누지 않는다.** 나눈 조각마다 무엇을 했는지 다시 써야 하고, 그것은 의미
판단이다. 포함된 rawId 와 시간 구간, 나눌 자리를 넘기고 나누는 것은 Repair 가 한다.

eventType 은 보지 않는다. `MOVEMENT` 는 지속시간 검사에서 빠지지만(`duration_guard`) 이
검사는 구조를 보는 것이라 타입과 무관하다.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo

from app.core.logging import get_logger, log_fields
from app.schemas import (
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.location_metrics import COVERAGE_GAP_MIN, SHORT_STAY_MAX
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

_WARNING_ID_PREFIX = "warning-movement-stay-"

_MOVEMENT = "MOVEMENT"
_STAY = "STAY"


@dataclass(frozen=True)
class SourceSpan:
    """입력 항목 하나의 시간 구간."""

    raw_id: str
    start: datetime
    end: datetime
    place: str | None = None
    #: 입력의 종류. 체류인지 이동인지.
    kind: str = _STAY

    @property
    def minutes(self) -> int:
        return round((self.end - self.start).total_seconds() / 60)

    @property
    def is_long_stay(self) -> bool:
        return self.kind == _STAY and self.end - self.start > SHORT_STAY_MAX

    def as_dict(self) -> dict[str, str | int]:
        payload: dict[str, str | int] = {
            "rawId": self.raw_id,
            "startAt": self.start.isoformat(),
            "endAt": self.end.isoformat(),
            "minutes": self.minutes,
        }
        if self.place:
            payload["place"] = self.place
        return payload


@dataclass(frozen=True)
class MovementStayViolation:
    """이동 사이에 낀 장시간 체류 하나."""

    stay: SourceSpan
    movements_before: tuple[SourceSpan, ...]
    movements_after: tuple[SourceSpan, ...]

    def raw_ids(self) -> list[str]:
        return [
            *(span.raw_id for span in self.movements_before),
            self.stay.raw_id,
            *(span.raw_id for span in self.movements_after),
        ]


@dataclass(frozen=True)
class EventViolation:
    """event 하나가 품은 위반 전체. event 하나에 장시간 체류가 여럿일 수 있다."""

    event: TimelineEventDraft
    violations: tuple[MovementStayViolation, ...]
    #: event 가 근거로 댄 체류·이동 전체. 시간순이다.
    sources: tuple[SourceSpan, ...]

    def detail(self) -> dict[str, object]:
        """Repair 에 넘기는 검사 결과. 같은 사실을 프롬프트에서 다시 계산하지 않게 한다.

        `segments` 는 나눌 자리다. event 가 근거로 댄 체류와 이동을 시간순으로 놓고, 이어진
        이동과 20분 이하 체류는 하나의 이동으로, 20분을 넘는 체류는 따로 묶은 것이다.
        **event 의 시간 안으로 맞춘 값이다.** 요청 시간 범위 끝에서 잘린 event 는 근거가
        그 뒤까지 이어져 있어, 근거의 시간을 그대로 주면 Repair 가 event 밖으로 나가는
        조각을 만든다. 통째로 밖에 있는 근거는 `outsideEventRawIds` 로 따로 알린다.

        `shortStays` 는 이동과 이어지지 않은 20분 이하 체류다. 나눌 자리가 아니라서
        `segments` 에 넣지 않는다. 있을 때만 싣는다.
        """

        layout = _layout(self.event, self.sources)
        detail: dict[str, object] = {
            "eventStartTime": self.event.start_time.isoformat(),
            "eventEndTime": self.event.end_time.isoformat(),
            "longStays": [violation.stay.as_dict() for violation in self.violations],
            "segments": layout.segments,
        }
        if layout.short_stays:
            detail["shortStays"] = layout.short_stays
        detail["outsideEventRawIds"] = layout.outside
        return detail


@dataclass(frozen=True)
class _LocationIndex:
    stays: dict[str, SourceSpan]
    movements: dict[str, SourceSpan]


def _index(request: TimelineDraftRequest, tz: tzinfo) -> _LocationIndex:
    """구간을 읽을 수 있는 STAY·MOVEMENT 만 rawId 로 찾을 수 있게 모은다."""

    def spans(items, kind: str) -> dict[str, SourceSpan]:
        found: dict[str, SourceSpan] = {}
        for item in items:
            identifier = raw_id_of(item)
            if not identifier or not item.end_at:
                continue
            start = parse_datetime(item.start_at, tz)
            end = parse_datetime(item.end_at, tz)
            if start is None or end is None or end <= start:
                continue
            place = (item.place or item.address) if kind == _STAY else None
            found[identifier] = SourceSpan(identifier, start, end, place, kind)
        return found

    return _LocationIndex(
        stays=spans(request.stays, _STAY),
        movements=spans(request.movements, _MOVEMENT),
    )


def _referenced(raw_ids: Iterable[str], index: _LocationIndex) -> list[SourceSpan]:
    """근거로 댄 체류·이동을 시간순으로."""

    wanted = {str(raw_id) for raw_id in raw_ids}
    found = [
        span
        for raw_id, span in {**index.stays, **index.movements}.items()
        if raw_id in wanted
    ]
    return sorted(found, key=lambda span: (span.start, span.end))


def _violations(sources: list[SourceSpan]) -> list[MovementStayViolation]:
    movements = [span for span in sources if span.kind == _MOVEMENT]
    if len(movements) < 2:
        return []

    found: list[MovementStayViolation] = []
    for stay in (span for span in sources if span.is_long_stay):
        before = tuple(span for span in movements if span.start < stay.start)
        after = tuple(
            span for span in movements if span not in before and span.end > stay.end
        )
        if before and after:
            found.append(MovementStayViolation(stay, before, after))
    return found


@dataclass(frozen=True)
class _Placed:
    """event 의 시간 안으로 맞춘 근거 하나."""

    raw_id: str
    start: datetime
    end: datetime
    place: str | None
    kind: str
    #: 길이는 원본 구간으로 잰다. event 끝에서 잘린 체류도 장시간 체류다.
    is_long_stay: bool

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "rawId": self.raw_id,
            "startAt": self.start.isoformat(),
            "endAt": self.end.isoformat(),
        }
        if self.place:
            payload["place"] = self.place
        return payload


@dataclass(frozen=True)
class _Layout:
    """event 의 시간 안에서 근거가 놓인 모양."""

    segments: list[dict[str, object]]
    #: 이동과 이어지지 않은 20분 이하 체류. 나눌 자리가 아니다.
    short_stays: list[dict[str, object]]
    #: event 의 시간 밖에 통째로 있는 근거.
    outside: list[str]


def _runs(spans: list[_Placed]) -> list[list[_Placed]]:
    """수집이 끊기지 않고 이어진 근거끼리 묶는다.

    `COVERAGE_GAP_MIN` 이상 기록이 없으면 끊긴 것으로 본다. 끊긴 구간을 사이에 둔 두
    근거는 이어진 이동이 아니다.
    """

    runs: list[list[_Placed]] = []
    reached: datetime | None = None
    for span in spans:
        if reached is None or span.start - reached >= COVERAGE_GAP_MIN:
            runs.append([])
        runs[-1].append(span)
        reached = span.end if reached is None else max(reached, span.end)
    return runs


def _layout(event: TimelineEventDraft, sources: tuple[SourceSpan, ...]) -> _Layout:
    """event 의 시간 안에서 나눌 자리를 계산한다.

    이어진 이동과 그 사이의 20분 이하 체류는 하나의 이동이다. 실제 draft 에서 한 event 가
    하루의 위치 기록을 거의 다 근거로 댄 적이 있는데, 시간이 이어지는지 보지 않고 묶었더니
    오전의 8분짜리 체류와 밤의 이동이 11시간짜리 이동 하나가 됐다. 그래서 수집이 끊기지
    않은 근거끼리만 묶고, 이동과 이어지지 않은 짧은 체류는 나눌 자리에서 뺀다.
    """

    inside: list[_Placed] = []
    outside: list[str] = []
    for span in sources:
        start = max(span.start, event.start_time)
        end = min(span.end, event.end_time)
        if end <= start:
            outside.append(span.raw_id)
            continue
        inside.append(
            _Placed(span.raw_id, start, end, span.place, span.kind, span.is_long_stay)
        )

    segments: list[dict[str, object]] = []
    short_stays: list[dict[str, object]] = []

    def close(journey: list[_Placed]) -> None:
        if not journey:
            return
        if all(span.kind == _STAY for span in journey):
            short_stays.extend(span.as_dict() for span in journey)
            return
        segments.append(
            {
                "kind": _MOVEMENT,
                "startAt": min(span.start for span in journey).isoformat(),
                "endAt": max(span.end for span in journey).isoformat(),
                "rawIds": [span.raw_id for span in journey],
            }
        )

    for run in _runs(inside):
        journey: list[_Placed] = []
        for span in run:
            if not span.is_long_stay:
                journey.append(span)
                continue
            close(journey)
            journey = []
            segment: dict[str, object] = {
                "kind": _STAY,
                "startAt": span.start.isoformat(),
                "endAt": span.end.isoformat(),
                "rawIds": [span.raw_id],
            }
            if span.place:
                segment["place"] = span.place
            segments.append(segment)
        close(journey)

    return _Layout(segments=segments, short_stays=short_stays, outside=outside)


def find_movement_stay_violations(
    raw_ids: Iterable[str], request: TimelineDraftRequest
) -> list[MovementStayViolation]:
    """근거 rawId 묶음 하나가 이동 사이의 장시간 체류를 함께 품고 있는지 본다.

    candidate 와 draft event 가 함께 쓴다. 두 곳이 다른 규칙으로 보면 candidate 에서는
    통과한 구조가 draft 에서 걸리거나 그 반대가 된다.
    """

    tz = resolve_timezone(request.timezone)
    return _violations(_referenced(raw_ids, _index(request, tz)))


def verify_movement_stay_boundary(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> list[EventViolation]:
    """draft 의 event 를 검사해 위반을 warning 으로 남기고 돌려준다.

    반복마다 자기 이전 warning 을 지우고 다시 잰다. Repair 가 event 를 나눈 뒤에도 앞
    회차의 지적이 남으면 이미 해소한 위반을 또 고치려 든다.
    """

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]
    if not request.stays or len(request.movements) < 2:
        return []

    tz = resolve_timezone(request.timezone)
    index = _index(request, tz)

    found: list[EventViolation] = []
    sequence = 0
    for event in draft.events:
        sources = _referenced((ref.raw_id for ref in event.source_refs), index)
        violations = _violations(sources)
        if not violations:
            continue

        found.append(EventViolation(event, tuple(violations), tuple(sources)))
        for violation in violations:
            sequence += 1
            involved = set(violation.raw_ids())
            draft.warnings.append(
                TimelineWarning(
                    warning_id=f"{_WARNING_ID_PREFIX}{sequence:03d}",
                    severity=TimelineWarningSeverity.HIGH,
                    message=(
                        f"'{event.title}' event 가 이동 사이에 {violation.stay.minutes}분 "
                        "머문 체류를 함께 품고 있습니다. 20분을 넘는 체류는 앞 이동·체류·"
                        "뒤 이동으로 나눠야 합니다."
                    ),
                    source_refs=[
                        ref for ref in event.source_refs if ref.raw_id in involved
                    ],
                )
            )

    if found:
        # rawId 와 제목은 남기지 않는다. 어느 event 인지는 Langfuse 의 draft 에서 본다.
        logger.debug(
            "이동 사이 장시간 체류 검출",
            extra=log_fields(movementStayViolationCount=sequence),
        )
    return found
