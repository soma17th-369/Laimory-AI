"""event 의 장소 출력(`place` / `address`) 확정.

`place` 은 사용자가 알아보는 **실제 장소**여야 한다. `두꺼비 감자탕 지산점`,
`서울드래곤시티` 같은 상호·건물명이거나 `집`, `학교`, `회사` 같은 친숙한 생활 장소다.
`한 곳`, `근처`, `주변` 은 장소가 아니라 얼버무림이다.

`address` 는 **입력 근거에 실제 주소 문자열이 있을 때만** 채운다. 좌표를 보고 주소를
지어내면 안 된다 — 좌표는 프롬프트에 실리지도 않는다(이슈 #80).

## LLM 값을 덮어쓰지 않는 이유

`place` 을 근거의 `place` 로 **항상** 덮어쓰면 안 된다. live 출력의
`배스킨라빈스` 는 어떤 입력 필드에도 없다. 사진 속 영수증을 vision 이 읽어 낸
값이라 코드가 다시 만들어 낼 수 없다. 그래서 규칙은 이렇다.

    - `place`: 비어 있거나 얼버무림일 때만 근거의 `place` 로 채운다.
      채울 근거가 없으면 얼버무림을 그냥 지운다(없는 장소를 지어내는 것보다 낫다).
    - `address`  : 비어 있으면 근거의 주소로 채운다. 근거로 뒷받침되지 않는 주소는
      LLM 이 지어낸 것이므로 지우고 경고한다.

`place` 후보는 사용자가 정한 우선순위 STAY → MOVEMENT → PHOTO → CALENDAR 순으로 찾는다.
PHOTO 의 장소는 안 들어올 수 있고, 그때는 그냥 후보를 내놓지 않는다.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.schemas import (
    AgentEventResult,
    CalendarItem,
    MovementItem,
    PhotoItem,
    SourceRef,
    StayItem,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.place_text import normalize_place_text, place_text_contains
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: 장소가 아니라 얼버무림인 표현들. place 로 두지 않는다.
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
#: 이것은 정확한 주소가 아니므로 `address` 로 쓰지 않는다(place 로는 쓸 수 있다).
_APPROXIMATE_MARKERS = ("인근", "부근", "근처", "주변", "일대")

_MAX_EXAMPLES = 3

#: 이 모듈이 남기는 warning 의 id 접두어. 반복마다 자기 것만 골라 지우기 위해 쓴다.
_WARNING_PREFIX = "warning-place-"


@dataclass(frozen=True)
class _Evidence:
    """rawId 로 되짚을 수 있는 장소 근거."""

    stays: dict[str, StayItem]
    movements: dict[str, MovementItem]
    photos: dict[str, PhotoItem]
    calendars: dict[str, CalendarItem]


def _collect(request: TimelineDraftRequest) -> _Evidence:
    return _Evidence(
        stays={
            identifier: item
            for item in request.stays
            if (identifier := raw_id_of(item))
        },
        movements={
            identifier: item
            for item in request.movements
            if (identifier := raw_id_of(item))
        },
        photos={
            identifier: item
            for item in request.photos
            if (identifier := raw_id_of(item))
        },
        calendars={
            identifier: item
            for item in request.calendars
            if (identifier := raw_id_of(item))
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


def _referenced(refs: list[SourceRef], lookup: dict) -> Iterator:
    """`rawId` 로 입력 항목을 찾는다.

    `ref.source_type` 은 **보지 않는다.** rawId 는 UUID 라 타입을 가로질러 유일하고 각
    lookup 에는 그 타입의 rawId 만 들어 있으므로, `raw_id in lookup` 하나로 "그 입력이
    stay 다" 까지 보장된다. `source_lookup` 이 정리해 둔 "LLM 이 붙인 타입 라벨을 믿지 말고
    rawId 로 입력을 찾아 그 입력의 타입을 믿는다" 와 같은 원칙이다.

    draft 는 `normalize_source_types` 가 타입을 미리 정정해 주지만 **candidate 단계에는
    그 정정이 없다.** 타입 조건을 함께 보면 Event Agent 가 라벨을 틀린 후보는 근거를 영영
    찾지 못한다.
    """

    for ref in refs:
        item = lookup.get(ref.raw_id)
        if item is not None:
            yield item


def _stay_places(stay: StayItem) -> Iterator[str | None]:
    yield stay.place
    yield from stay.places


def _movement_places(movement: MovementItem) -> Iterator[str | None]:
    # 이동은 도착지가 그 event 의 장소를 더 잘 설명한다.
    for geo in (movement.end, movement.start):
        if geo is not None:
            yield geo.place
            yield from geo.places


def _calendar_places(calendar: CalendarItem) -> Iterator[str | None]:
    yield calendar_place_label(calendar.location_text)


def _stay_addresses(stay: StayItem) -> Iterator[str | None]:
    yield stay.address


def _movement_addresses(movement: MovementItem) -> Iterator[str | None]:
    for geo in (movement.end, movement.start):
        if geo is not None:
            yield geo.address


def _movement_destination_places(movement: MovementItem) -> Iterator[str | None]:
    """이동에서 **도착지 이름만** 내놓는다(candidate 전용).

    draft 쪽 `_movement_places` 와 달리 출발지를 섞지 않는다. candidate 의 `places` 는
    Timeline 이 User Memory 와 대조할 후보인데, 출발지가 섞여 있으면 `집` 을 보고
    "이 이동의 장소는 집" 으로 읽어 **집에서 나온 것을 집에 있었던 것으로 뒤집는다.**
    """

    if movement.end is not None:
        yield movement.end.place
        yield from movement.end.places


def _movement_destination_addresses(movement: MovementItem) -> Iterator[str | None]:
    """이동에서 **도착지 주소만** 내놓는다(candidate 전용).

    출발지로 넘어가면 `place` 는 도착지인데 `address` 는 출발지가 되어 짝이 어긋난다.
    도착지 주소가 근사값(`인근`)이면 채우지 않고 비운다 — 다른 지점의 정확한 주소보다
    빈 값이 낫다.
    """

    if movement.end is not None:
        yield movement.end.address


def _photo_places(photo: PhotoItem) -> Iterator[str | None]:
    yield from photo.places


def _photo_addresses(photo: PhotoItem) -> Iterator[str | None]:
    yield photo.address


def _calendar_address_support(calendar: CalendarItem) -> Iterator[str | None]:
    # 캘린더 메모(`집(경기도 오산시 운암로 90)`)는 그대로 address 로 쓰기엔 지저분하지만,
    # 주소를 품고 있으므로 검증 근거로는 쓴다.
    yield calendar.location_text


#: 장소명 후보를 찾는 순서. 사용자가 정한 우선순위이며 `_Evidence` 의 필드명과 짝을 이룬다.
#:
#: PHOTO 가 MOVEMENT 다음·CALENDAR 앞인 이유(이슈 #80): 사진 장소는 실제로 거기 있었다는
#: 증거이고, 캘린더 `locationText` 는 사용자가 적어 둔 의도라 실제와 다를 수 있다.
_PLACE_SOURCES: tuple[tuple[str, Callable[[Any], Iterator[str | None]]], ...] = (
    ("stays", _stay_places),
    ("movements", _movement_places),
    ("photos", _photo_places),
    ("calendars", _calendar_places),
)

#: `address` 로 그대로 쓸 수 있는 실제 주소 문자열의 출처.
_ADDRESS_SOURCES: tuple[tuple[str, Callable[[Any], Iterator[str | None]]], ...] = (
    ("stays", _stay_addresses),
    ("movements", _movement_addresses),
    ("photos", _photo_addresses),
)

#: 주소가 근거에 실재하는지 대조할 때만 추가로 보는 출처.
_ADDRESS_SUPPORT_SOURCES: tuple[tuple[str, Callable[[Any], Iterator[str | None]]], ...] = (
    ("calendars", _calendar_address_support),
)

#: candidate 전용 출처. draft 와 다른 점은 **이동에서 도착지만 본다**는 것뿐이다.
#:
#: draft 의 `place` 은 `_first` 로 하나만 고르므로 출발지가 뒤에 있어도 도착지가
#: 이긴다. 하지만 candidate 의 `places` 는 **목록 전체**가 Timeline 으로 가고 `address` 는
#: 도착지 주소가 근사값이면 출발지로 넘어간다. 그래서 candidate 에서는 아예 도착지로
#: 좁힌다(이슈 #72).
_CANDIDATE_PLACE_SOURCES: tuple[tuple[str, Callable[[Any], Iterator[str | None]]], ...] = (
    ("stays", _stay_places),
    ("movements", _movement_destination_places),
    ("photos", _photo_places),
    ("calendars", _calendar_places),
)

_CANDIDATE_ADDRESS_SOURCES: tuple[tuple[str, Callable[[Any], Iterator[str | None]]], ...] = (
    ("stays", _stay_addresses),
    ("movements", _movement_destination_addresses),
    ("photos", _photo_addresses),
)


def _from_sources(
    refs: list[SourceRef],
    evidence: _Evidence,
    sources: tuple[tuple[str, Callable[[Any], Iterator[str | None]]], ...],
) -> Iterator[str | None]:
    for attr, extract in sources:
        for item in _referenced(refs, getattr(evidence, attr)):
            yield from extract(item)


def _place_label_candidates(refs: list[SourceRef], evidence: _Evidence) -> Iterator[str | None]:
    """사용자가 정한 우선순위대로 장소명 후보를 내놓는다."""

    yield from _from_sources(refs, evidence, _PLACE_SOURCES)


def _address_candidates(refs: list[SourceRef], evidence: _Evidence) -> Iterator[str | None]:
    """`address` 로 그대로 쓸 수 있는 실제 주소 문자열."""

    yield from _from_sources(refs, evidence, _ADDRESS_SOURCES)


def _address_support(refs: list[SourceRef], evidence: _Evidence) -> Iterator[str | None]:
    """LLM 이 쓴 주소가 근거에 실재하는지 확인할 때 대조할 문자열들."""

    yield from _address_candidates(refs, evidence)
    yield from _from_sources(refs, evidence, _ADDRESS_SUPPORT_SOURCES)


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
    """event 의 `place` / `address` 를 근거로 확정한다(in-place)."""

    evidence = _collect(request)
    filled_labels: list[str] = []
    cleared_labels: list[str] = []
    filled_addresses: list[str] = []
    invented_addresses: list[str] = []

    memory_text = _user_memory_text(request)
    unsupported_labels: list[str] = []

    for event in draft.events:
        refs = event.source_refs
        if is_vague_place_label(event.place):
            label = _first(_place_label_candidates(refs, evidence), reject_vague=True)
            if label:
                event.place = label
                filled_labels.append(f"{event.title} → {label}")
            elif event.place is not None:
                # 채울 장소명이 없다. 얼버무림을 남기느니 비운다.
                cleared_labels.append(f"{event.title}({event.place})")
                event.place = None
        elif not _label_is_supported(event.place, refs, evidence, memory_text):
            # 보존 검사(#72): Timeline 이 옮겨 적은 장소명이 정말 근거에서 왔는지 본다.
            # **지우지는 않는다** — 사진에서 읽은 상호명처럼 코드가 재현할 수 없는 값이
            # 있고, 지우면 그것까지 잃는다.
            unsupported_labels.append(f"{event.title}({event.place})")

        support = [text for text in _address_support(refs, evidence) if text]
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
                    for candidate in _address_candidates(refs, evidence)
                    if is_exact_address(candidate)
                ),
                None,
            )
            if address:
                event.address = address
                filled_addresses.append(event.title)

    # 반복마다 자기 이전 warning 을 지우고 다시 잰다. Repair 가 event 를 병합·삭제하면
    # 앞 회차의 지적이 사라진 event 를 가리키게 된다(`narrative_guard` 와 같은 방식).
    draft.warnings = [w for w in draft.warnings if not w.warning_id.startswith(_WARNING_PREFIX)]

    if invented_addresses:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_PREFIX}address-001",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"정확한 입력 근거가 없는 주소 {len(invented_addresses)}건을 지웠습니다: "
                    f"{_examples(invented_addresses)}"
                ),
            )
        )

    if unsupported_labels:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_PREFIX}label-001",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"입력 근거와 User Memory 어디에도 없는 장소명 {len(unsupported_labels)}건: "
                    f"{_examples(unsupported_labels)}"
                ),
            )
        )

    logger.debug(
        "장소 확정: place 보강=%d, place 제거=%d, address 보강=%d, "
        "address 제거=%d, 근거 없는 place=%d",
        len(filled_labels),
        len(cleared_labels),
        len(filled_addresses),
        len(invented_addresses),
        len(unsupported_labels),
    )


def resolve_candidate_places(
    result: AgentEventResult, request: TimelineDraftRequest
) -> None:
    """candidate 의 `places`/`address` 를 입력에서 그대로 복사한다(in-place, #72).

    Event Agent 가 이 필드에 무엇을 써 보냈든 입력에서 복사한 값이 이긴다. 변환도 해석도
    없고, `sourceRefs` 로 근거 입력을 찾아 문자열을 옮기기만 한다.

    `places` 를 줄이지 않는 이유: 한 지점에 이름이 여럿일 수 있는데(`강남파이낸스센터` /
    `스타벅스 강남점`), 어느 것이 사용자의 `회사` 인지는 User Memory 를 가진 Timeline 만
    판단할 수 있다. 여기서 하나로 줄이면 그 대조 기회를 없앤다. 목록은 근거가 확실한
    순서(STAY → MOVEMENT 도착지 → CALENDAR)로 담기므로 순서 자체가 우선순위다.
    """

    if not result.candidates:
        return

    evidence = _collect(request)
    filled_places = 0
    filled_addresses = 0
    for candidate in result.candidates:
        refs = candidate.source_refs
        labels = _unique_labels(
            _from_sources(refs, evidence, _CANDIDATE_PLACE_SOURCES)
        )
        candidate.places = labels
        candidate.address = next(
            (
                value.strip()
                for value in _from_sources(refs, evidence, _CANDIDATE_ADDRESS_SOURCES)
                if is_exact_address(value)
            ),
            None,
        )
        filled_places += bool(labels)
        filled_addresses += candidate.address is not None

    logger.debug(
        "후보 장소 복사: candidates=%d, place 채움=%d, address 채움=%d",
        len(result.candidates),
        filled_places,
        filled_addresses,
    )


def _unique_labels(values: Iterator[str | None]) -> list[str]:
    """장소명 후보를 순서를 지키며 디듀프한다. 얼버무림과 빈 값은 뺀다."""

    seen: set[str] = set()
    labels: list[str] = []
    for value in values:
        if not value or is_vague_place_label(value):
            continue
        stripped = value.strip()
        key = normalize_place_text(stripped)
        if key in seen:
            continue
        seen.add(key)
        labels.append(stripped)
    return labels


def _user_memory_text(request: TimelineDraftRequest) -> str:
    """User Memory 를 통째 문자열로 만든다(보존 검사 대조용).

    자연어 필드를 골라 읽지 않고 통째로 검색한다. `notification_guard` 와 같은 방식이며,
    결정론 코드가 User Memory 의 필드 이름이나 `customAttributes` 키에 구조적으로
    의존하지 않게 하려는 것이다(#65).
    """

    memory = request.user_memory
    if memory is None:
        return ""
    try:
        return json.dumps(memory.prompt_payload(), ensure_ascii=False)
    except (TypeError, ValueError):
        # 보조 context 하나 때문에 장소 확정을 멈추지 않는다.
        return ""


def _label_is_supported(
    label: str | None,
    refs: list[SourceRef],
    evidence: _Evidence,
    memory_text: str,
) -> bool:
    """장소명이 입력 근거나 User Memory 중 하나에서 왔는가.

    `집`·`회사` 같은 생활 장소명은 입력에 없고 User Memory 에만 있다. 그것을 붙이게 하는
    것이 #72 의 목적이므로 두 곳 중 하나면 통과시킨다.
    """

    if not label:
        return True
    normalized = normalize_place_text(label)
    if not normalized:
        return True
    for value in _place_label_candidates(refs, evidence):
        if value and normalize_place_text(value) == normalized:
            return True
    for value in _address_candidates(refs, evidence):
        if value and place_text_contains(value, label):
            return True
    return bool(memory_text) and normalized in normalize_place_text(memory_text)
