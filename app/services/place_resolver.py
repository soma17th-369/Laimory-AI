"""event 의 장소 출력(`placeLabel` / `address`) 확정.

`placeLabel` 은 사용자가 알아보는 **실제 장소**여야 한다. `두꺼비 감자탕 지산점`,
`서울드래곤시티` 같은 상호·건물명이거나 `집`, `학교`, `회사` 같은 친숙한 생활 장소다.
`한 곳`, `근처`, `주변` 은 장소가 아니라 얼버무림이다.

`address` 는 **입력 근거에 실제 주소 문자열이 있을 때만** 채운다. 사진에는 좌표만
있으므로, 좌표를 보고 주소를 지어내면 안 된다.

## LLM 값을 덮어쓰지 않는 이유

`placeLabel` 을 근거의 `place` 로 **항상** 덮어쓰면 안 된다. live 출력의
`배스킨라빈스` 는 어떤 입력 필드에도 없다. 사진 속 영수증을 vision 이 읽어 낸
값이라 코드가 다시 만들어 낼 수 없다. 그래서 규칙은 이렇다.

    - `placeLabel`: 비어 있거나 얼버무림일 때만 근거의 `place` 로 채운다.
      채울 근거가 없으면 얼버무림을 그냥 지운다(없는 장소를 지어내는 것보다 낫다).
    - `address`  : 비어 있으면 근거의 주소로 채운다. 근거로 뒷받침되지 않는 주소는
      LLM 이 지어낸 것이므로 지우고 경고한다.

`place` 후보는 사용자가 정한 우선순위 STAY → MOVEMENT → CALENDAR 순으로 찾는다.
PHOTO 는 `place` 필드가 없어(좌표뿐) 후보를 제공하지 못한다.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from app.core.logging import get_logger
from app.schemas import (
    CalendarItem,
    EventSourceType,
    MovementItem,
    StayItem,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.place_text import place_text_contains
from app.services.source_lookup import source_identifier

logger = get_logger(__name__)

#: 장소가 아니라 얼버무림인 표현들. placeLabel 로 두지 않는다.
_VAGUE_PLACE_LABELS = frozenset(
    {
        "한 곳",
        "한곳",
        "한 장소",
        "어떤 장소",
        "특정 장소",
        "근처",
        "주변",
        "부근",
        "알 수 없음",
        "미상",
        "unknown",
    }
)

#: 근사 위치를 뜻하는 꼬리말. 수집 원본이 `경기도 오산시 운암로 90 인근` 처럼 주는데,
#: 이것은 정확한 주소가 아니므로 `address` 로 쓰지 않는다(placeLabel 로는 쓸 수 있다).
_APPROXIMATE_MARKERS = ("인근", "부근", "근처", "주변", "일대")

_MAX_EXAMPLES = 3


@dataclass(frozen=True)
class _Evidence:
    """rawId 로 되짚을 수 있는 장소 근거."""

    stays: dict[str, StayItem]
    movements: dict[str, MovementItem]
    calendars: dict[str, CalendarItem]


def _collect(request: TimelineDraftRequest) -> _Evidence:
    return _Evidence(
        stays={
            identifier: item
            for item in request.stays
            if (identifier := source_identifier(item))
        },
        movements={
            identifier: item
            for item in request.movements
            if (identifier := source_identifier(item))
        },
        calendars={
            identifier: item
            for item in request.calendars
            if (identifier := source_identifier(item))
        },
    )


def is_vague_place_label(label: str | None) -> bool:
    """장소명이 아니라 얼버무림인가."""

    if label is None:
        return True
    stripped = label.strip()
    return not stripped or stripped.lower() in _VAGUE_PLACE_LABELS


def calendar_place_label(location_text: str | None) -> str | None:
    """캘린더 장소 메모에서 사람이 부르는 라벨만 떼어 낸다.

    `집(경기도 오산시 운암로 90)` → `집`. 괄호가 없으면 메모 전체를 라벨로 본다.
    """

    if not location_text:
        return None
    label = location_text.split("(", 1)[0].strip()
    return label or location_text.strip() or None


def is_exact_address(text: str | None) -> bool:
    """`address` 로 쓸 수 있는 정확한 주소인가.

    `경기도 오산시 운암로 90 인근` 처럼 근사를 뜻하는 꼬리말이 붙으면 정확한 주소가
    아니다. 사용자에게 보이는 주소는 지도에서 찾을 수 있는 값이어야 한다.
    """

    if not text or not text.strip():
        return False
    return not any(marker in text for marker in _APPROXIMATE_MARKERS)


def _referenced(event: TimelineEventDraft, source_type: EventSourceType, lookup: dict) -> Iterator:
    for ref in event.source_refs:
        if ref.source_type is source_type and ref.source_id in lookup:
            yield lookup[ref.source_id]


def _place_label_candidates(event: TimelineEventDraft, evidence: _Evidence) -> Iterator[str]:
    """사용자가 정한 우선순위대로 장소명 후보를 내놓는다."""

    for stay in _referenced(event, EventSourceType.STAY, evidence.stays):
        yield from (stay.place, *stay.places)

    # 이동은 도착지가 그 event 의 장소를 더 잘 설명한다.
    for movement in _referenced(event, EventSourceType.MOVEMENT, evidence.movements):
        for geo in (movement.end, movement.start):
            if geo is not None:
                yield from (geo.place, *geo.places)

    for calendar in _referenced(event, EventSourceType.CALENDAR, evidence.calendars):
        yield calendar_place_label(calendar.location_text)

    # PHOTO 에는 place 필드가 없다(좌표뿐). 사진에서 읽은 상호명은 LLM 만 알 수 있다.


def _address_candidates(event: TimelineEventDraft, evidence: _Evidence) -> Iterator[str]:
    """`address` 로 그대로 쓸 수 있는 실제 주소 문자열."""

    for stay in _referenced(event, EventSourceType.STAY, evidence.stays):
        yield stay.address
    for movement in _referenced(event, EventSourceType.MOVEMENT, evidence.movements):
        for geo in (movement.end, movement.start):
            if geo is not None:
                yield geo.address


def _address_support(event: TimelineEventDraft, evidence: _Evidence) -> Iterator[str]:
    """LLM 이 쓴 주소가 근거에 실재하는지 확인할 때 대조할 문자열들.

    캘린더 메모(`집(경기도 오산시 운암로 90)`)는 그대로 address 로 쓰기엔 지저분하지만,
    주소를 품고 있으므로 검증 근거로는 쓴다.
    """

    yield from _address_candidates(event, evidence)
    for calendar in _referenced(event, EventSourceType.CALENDAR, evidence.calendars):
        yield calendar.location_text


def _first(values: Iterator[str | None], reject_vague: bool = False) -> str | None:
    for value in values:
        if not value or not value.strip():
            continue
        if reject_vague and is_vague_place_label(value):
            continue
        return value.strip()
    return None


def _examples(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_EXAMPLES])
    if len(items) > _MAX_EXAMPLES:
        shown += f" 외 {len(items) - _MAX_EXAMPLES}건"
    return shown


def resolve_places(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """event 의 `placeLabel` / `address` 를 근거로 확정한다(in-place)."""

    evidence = _collect(request)
    filled_labels: list[str] = []
    cleared_labels: list[str] = []
    filled_addresses: list[str] = []
    invented_addresses: list[str] = []

    for event in draft.events:
        if is_vague_place_label(event.place_label):
            label = _first(_place_label_candidates(event, evidence), reject_vague=True)
            if label:
                event.place_label = label
                filled_labels.append(f"{event.title} → {label}")
            elif event.place_label is not None:
                # 채울 장소명이 없다. 얼버무림을 남기느니 비운다.
                cleared_labels.append(f"{event.title}({event.place_label})")
                event.place_label = None

        support = [text for text in _address_support(event, evidence) if text]
        if event.address and not (
            is_exact_address(event.address)
            and any(place_text_contains(event.address, text) for text in support)
        ):
            invented_addresses.append(f"{event.title}({event.address})")
            event.address = None

        if not event.address:
            address = next(
                (
                    candidate.strip()
                    for candidate in _address_candidates(event, evidence)
                    if is_exact_address(candidate)
                ),
                None,
            )
            if address:
                event.address = address
                filled_addresses.append(event.title)

    if invented_addresses:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-place-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"정확한 입력 근거가 없는 주소 {len(invented_addresses)}건을 지웠습니다: "
                    f"{_examples(invented_addresses)}"
                ),
            )
        )

    logger.info(
        "장소 확정: placeLabel 보강=%d, placeLabel 제거=%d, address 보강=%d, address 제거=%d",
        len(filled_labels),
        len(cleared_labels),
        len(filled_addresses),
        len(invented_addresses),
    )
