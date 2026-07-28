"""이동 없이 이어진 체류(STAY) 묶기.

위치 수집은 자주 끊긴다. 사람이 집에서 나가지 않았는데도 STAY 가 `11:12~11:20`,
`14:26~14:36` 처럼 몇 분짜리 조각으로 흩어져 들어온다. Timeline Agent 는 이 조각을
그대로 받아 8분·9분짜리 event 두 개를 만든다. 그 사이 세 시간은 하루에서 사라진다.

사이에 **이동 기록이 없다면** 사람은 그 자리에 계속 있었던 것이다. 같은 장소를 가리키는
연속한 STAY 는 하나의 체류로 본다. 이 모듈은 입력만 보고 "같이 묶어야 할 STAY rawId
집합"을 계산한다. draft event 를 실제로 병합하는 것은 `draft_repair` 다.

병합 조건(모두 참일 때만):

    - 장소가 같다. `place` 를 정규화해 완전히 일치하거나, 둘 다 `place` 가 없으면
      `address` 가 일치한다.
    - 사이에 MOVEMENT 기록이 없다.
    - 사이에 수면 구간이 없다. 자고 일어난 것은 이어진 체류가 아니라 하루의 경계다.

장소 비교에 `places_match()` 를 쓰지 않는 이유가 있다. 그 함수는 토큰이 하나만 겹쳐도
참이라 `오산시` 수준에서 서로 다른 장소를 묶어 버린다(함수 docstring 이 스스로
경고한다). 여기서는 아직 같은 event 로 묶이지 않은 별개의 STAY 두 개를 비교하므로
완전 일치가 아니면 묶지 않는다. 그래서 `오산운암3단지 주공아파트` 와
`오산운암3단지 주공아파트 인근`(산책하러 나간 곳)은 서로 다른 장소로 남는다.

좌표는 쓰지 않는다. 실제 데이터에서 같은 아파트의 STAY 끼리도 좌표가 212m 까지
벌어져, 거리 임계값으로는 같은 곳인지 가릴 수 없다.
"""

from dataclasses import dataclass
from datetime import datetime, tzinfo

from app.core.logging import get_logger
from app.schemas import TimelineDraftRequest
from app.services.place_text import normalize_place_text
from app.services.sleep_guard import sleep_spans
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime

logger = get_logger(__name__)

Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class _Stay:
    """병합 판정에 필요한 것만 추린 체류."""

    identifier: str
    start: datetime
    end: datetime
    place: str | None
    address: str | None


def _same_place(left: _Stay, right: _Stay) -> bool:
    """두 체류가 같은 장소인가. 장소명이 원칙이고, 없을 때만 주소로 판정한다."""

    if left.place and right.place:
        return normalize_place_text(left.place) == normalize_place_text(right.place)
    if left.place or right.place:
        return False  # 한쪽만 이름이 있으면 같은 곳이라 단정할 수 없다
    if left.address and right.address:
        return normalize_place_text(left.address) == normalize_place_text(right.address)
    return False


def _intersects(spans: list[Span], start: datetime, end: datetime) -> bool:
    """구간 목록 중 [start, end] 와 시간이 겹치는 것이 있는가."""

    return any(
        span_start < end and start < span_end for span_start, span_end in spans
    )


def _movement_spans(request: TimelineDraftRequest, tz: tzinfo) -> list[Span]:
    """이동 기록의 시간 구간. 종료 시각이 없으면 시작 시각의 순간으로 본다."""

    spans: list[Span] = []
    for movement in request.movements:
        start = parse_datetime(movement.start_at, tz)
        if start is None:
            continue
        end = parse_datetime(movement.end_at, tz) if movement.end_at else None
        spans.append((start, max(end, start) if end else start))
    return spans


def _stays(request: TimelineDraftRequest, tz: tzinfo) -> list[_Stay]:
    """식별자와 시간을 아는 체류만 시간순으로."""

    stays: list[_Stay] = []
    for item in request.stays:
        identifier = raw_id_of(item)
        start = parse_datetime(item.start_at, tz)
        if not identifier or start is None:
            continue
        end = parse_datetime(item.end_at, tz) if item.end_at else None
        stays.append(
            _Stay(
                identifier=identifier,
                start=start,
                end=max(end, start) if end else start,
                place=item.place,
                address=item.address,
            )
        )
    return sorted(stays, key=lambda stay: (stay.start, stay.end))


def mergeable_stay_groups(
    request: TimelineDraftRequest, tz: tzinfo
) -> list[frozenset[str]]:
    """하나의 체류로 묶어야 할 STAY rawId 집합들. 2건 이상인 묶음만 반환한다."""

    stays = _stays(request, tz)
    if len(stays) < 2:
        return []

    blockers = [*_movement_spans(request, tz), *sleep_spans(request, tz)]

    groups: list[list[_Stay]] = [[stays[0]]]
    for stay in stays[1:]:
        previous = groups[-1][-1]
        gap_start, gap_end = previous.end, stay.start
        joins = (
            _same_place(previous, stay)
            # 앞 체류가 끝난 뒤에 시작한 별개의 조각이어야 한다. 시간이 겹치는 두 STAY 는
            # 끊긴 수집이 아니라 중복 기록이다. 그런 event 는 `resolve_overlaps` 가 본다.
            and gap_start <= gap_end
            and not _intersects(blockers, gap_start, gap_end)
        )
        if joins:
            groups[-1].append(stay)
        else:
            groups.append([stay])

    merged = [
        frozenset(stay.identifier for stay in group) for group in groups if len(group) > 1
    ]
    if merged:
        logger.info("이동 없이 이어진 체류 묶음 %d건을 찾았습니다.", len(merged))
    return merged
