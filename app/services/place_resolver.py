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

## 표시 장소 우선순위 (#67)

    1. 사용자 생활 장소명(`집`·`회사`·`학교`). User Memory 가 알려 준 장소와 근거의
       장소·주소가 맞으면 사용자가 부르는 이름으로 보여 준다.
    2. 직접 확인된 상호·건물·시설명. STAY → MOVEMENT 도착지 → CALENDAR 순이다.
    3. 정확한 주소 fallback. 이름을 모르지만 주소는 아는 event 를 장소 없는 event 로
       두지 않는다.

근거가 하나도 없으면 만들어 내지 않고 `null` 을 유지한다. 장소를 모르는 것은 정상이고,
지어낸 장소는 사용자가 자기 하루를 의심하게 만든다.

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
from app.services.source_lookup import raw_id_of

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

#: User Memory 의 key 에서 생활 장소를 알아보는 표지. 메모리는 비정형 JSON 이라
#: 고정 스키마가 없어, key 이름에 이 말이 들어 있으면 그 값을 장소 문자열로 본다.
#: 값이 장소처럼 생기지 않았으면 어차피 근거와 맞지 않아 라벨이 붙지 않는다.
_LIVING_PLACE_KEYS: dict[str, tuple[str, ...]] = {
    "집": ("home", "집", "residence", "house"),
    "회사": ("work", "회사", "office", "company", "workplace"),
    "학교": ("school", "학교", "university", "campus"),
}


def living_place_map(user_memory) -> dict[str, list[str]]:
    """User Memory 에서 `생활 장소명 → 장소 문자열들` 을 뽑는다.

    메모리는 자유 구조라 어느 key 에 있을지 모른다. 재귀로 훑으며 key 이름이 생활
    장소를 가리키고 값이 문자열이면 후보로 담는다. 맞는 것이 없으면 빈 map 이고,
    그때는 기존 동작(근거의 장소명 사용)이 그대로 유지된다.
    """

    if user_memory is None:
        return {}

    found: dict[str, list[str]] = {}

    def walk(node: object, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, str(key))
            return
        if isinstance(node, list):
            for value in node:
                walk(value, key_hint)
            return
        if not isinstance(node, str) or not node.strip():
            return
        hint = key_hint.casefold()
        for label, markers in _LIVING_PLACE_KEYS.items():
            if any(marker in hint for marker in markers):
                found.setdefault(label, []).append(node.strip())

    walk(user_memory.model_dump(by_alias=True, mode="json"))
    return found


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
            if (identifier := raw_id_of(item))
        },
        movements={
            identifier: item
            for item in request.movements
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


def _referenced(event: TimelineEventDraft, source_type: EventSourceType, lookup: dict) -> Iterator:
    for ref in event.source_refs:
        if ref.source_type is source_type and ref.raw_id in lookup:
            yield lookup[ref.raw_id]


def _evidence_place_texts(
    event: TimelineEventDraft, evidence: _Evidence
) -> Iterator[str]:
    """이 event 의 근거가 말하는 장소·주소 문자열 전부. 생활 장소 대조에 쓴다."""

    for stay in _referenced(event, EventSourceType.STAY, evidence.stays):
        yield from (text for text in (stay.place, stay.address, *stay.places) if text)
    for movement in _referenced(event, EventSourceType.MOVEMENT, evidence.movements):
        for geo in (movement.end, movement.start):
            if geo is not None:
                yield from (text for text in (geo.place, geo.address, *geo.places) if text)
    for calendar in _referenced(event, EventSourceType.CALENDAR, evidence.calendars):
        if calendar.location_text:
            yield calendar.location_text


def _living_place_label(
    event: TimelineEventDraft,
    evidence: _Evidence,
    living: dict[str, list[str]],
) -> str | None:
    """근거의 장소가 사용자 생활 장소와 같으면 사용자가 부르는 이름을 돌려준다."""

    if not living:
        return None
    texts = list(_evidence_place_texts(event, evidence))
    if not texts:
        return None
    for label, phrases in living.items():
        for phrase in phrases:
            if any(place_text_contains(text, phrase) for text in texts):
                return label
    return None


def _place_label_candidates(
    event: TimelineEventDraft,
    evidence: _Evidence,
    living: dict[str, list[str]] | None = None,
) -> Iterator[str]:
    """사용자가 정한 우선순위대로 장소명 후보를 내놓는다."""

    # 1순위: 사용자가 부르는 생활 장소명. `오산운암3단지 주공아파트` 보다 `집` 이 낫다.
    memory_label = _living_place_label(event, evidence, living or {})
    if memory_label:
        yield memory_label

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
    living = living_place_map(request.user_memory)
    filled_labels: list[str] = []
    cleared_labels: list[str] = []
    filled_addresses: list[str] = []
    invented_addresses: list[str] = []

    for event in draft.events:
        if is_vague_place_label(event.place_label):
            label = _first(
                _place_label_candidates(event, evidence, living), reject_vague=True
            )
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

        # 이름은 모르지만 주소는 아는 event 를 장소 없는 event 로 두지 않는다(#67).
        # 우선순위의 마지막 칸이다 — 앞의 두 단계가 모두 빈손일 때만 온다.
        if not event.place_label and event.address:
            event.place_label = event.address
            filled_labels.append(f"{event.title} → {event.address}")

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

    logger.debug(
        "장소 확정: placeLabel 보강=%d, placeLabel 제거=%d, address 보강=%d, address 제거=%d",
        len(filled_labels),
        len(cleared_labels),
        len(filled_addresses),
        len(invented_addresses),
    )
