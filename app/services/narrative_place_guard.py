"""사용자 문장의 데이터 라벨·주소·시각 정리 (#138).

`title`·`description` 은 사용자가 읽는 일기다. 그런데 Timeline 은 입력 주소를 그대로
옮겨(`서울특별시 예시구 예시로 123-4에서 보낸 시간`) 쓰거나, 센서 용어(`카페 체류`)와
시각(`자정 전 귀가`)을 제목에 쓴다.

둘로 나눠 다룬다. **무엇으로 바꿀지가 규칙으로 정해지면 코드가 바꾸고, 의미 판단이면 찾아서
Repair 에 넘긴다.**

    - 입력 주소 원문: event 가 근거로 댄 입력의 주소와 글자 그대로 같은 부분만 `place`
      로, `place` 가 없으면 그 주소의 시·군·구 이름으로 바꾼다. 입력 주소와 일치하는
      부분만 바꾸므로 장소명 안의 숫자(`2호선`, `63빌딩`)는 남는다.
    - `체류` 제목, 제목의 시각, 입력에 없는 주소 모양 문자열: 무엇으로 바꿀지(활동인지
      `~에서 보낸 시간` 인지)가 의미 판단이다. 찾아서 `findings` 로 남긴다. 주소 모양은
      정규식으로 짐작한 것이라 틀릴 수 있는데, 고치지 않고 짚기만 하므로 틀려도 Repair 가
      한 번 더 볼 뿐이다.

v3 세트에서만 돈다. v1·v2 프롬프트는 이 규칙을 말하지 않는다.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from app.core.logging import get_logger
from app.schemas import TimelineDraft, TimelineDraftRequest, TimelineEventDraft
from app.services.place_resolver import is_vague_place_label
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: 근사 위치 꼬리말. 입력 주소에 붙어 오면 떼어 낸 것도 함께 찾는다.
_APPROXIMATE_SUFFIX = re.compile(r"\s*(인근|부근|근처|주변|일대)\s*$")

#: 주소로 보기에는 너무 짧은 문자열. 이보다 짧으면 바꾸지 않는다(`서울` 같은 지역명).
_MIN_ADDRESS_LENGTH = 5

#: 주소의 시·군·구 토큰.
_DISTRICT = re.compile(r"^[가-힣]+(구|군|시)$")

#: 제목에 쓰지 않는 체류 표현(`장기 체류`·`재체류` 포함).
_STAY_WORD = re.compile(r"체류")

#: 제목에 쓰지 않는 시각·시간 경계. `밤`·`오전` 처럼 하루 중 때를 가리키는 말은 허용한다.
_TIME_IN_TITLE = re.compile(
    r"\d{1,2}\s?:\s?\d{2}|\d{1,2}\s?시|자정|정오|새벽\s?\d"
)

#: 입력에 없어도 주소로 보이는 모양. 단위가 붙은 숫자(`3층`, `2명`, `5시`)는 뺀다.
_UNIT_LOOKAHEAD = r"(?!\s?[층호명개시분년월일가번])"
_ADDRESS_SHAPES = (
    # 도로명 + 건물번호: `예시로 123-4`, `중앙로12번길 34`
    re.compile(r"[가-힣A-Za-z0-9]+(?:로|길)\s?\d+(?:-\d+)?" + _UNIT_LOOKAHEAD),
    # 지번: `역삼동 123-4`, `오산리 45번지`
    re.compile(r"[가-힣]+(?:동|리)\s?\d+(?:-\d+)?(?:번지)?" + _UNIT_LOOKAHEAD),
    # 동호수: `101동 1203호`
    re.compile(r"\d+동\s?\d+호"),
)


def _variants(address: str) -> Iterator[str]:
    """입력 주소와, 문장에 옮겨 적힐 수 있는 그 뒷부분.

    모델은 주소를 통째로 옮기기도 하고 앞의 시·도를 떼고 `예시로 123-4` 만 옮기기도 한다.
    뒷부분은 번호가 든 것만 낸다 — `예시구` 같은 지역명은 주소가 아니라 쓸 수 있는 말이다.
    """

    stripped = address.strip()
    bases = [stripped]
    without = _APPROXIMATE_SUFFIX.sub("", stripped)
    if without != stripped:
        bases.append(without)
    for base in bases:
        tokens = base.split()
        for start in range(len(tokens)):
            tail = tokens[start:]
            if len(tail) >= 2 and any(char.isdigit() for char in tail[-1]):
                yield " ".join(tail)


def _calendar_address(location_text: str | None) -> str | None:
    """`집(경기도 오산시 운암로 90)` 의 괄호 안. 괄호가 없으면 메모 전체다."""

    if not location_text:
        return None
    match = re.search(r"\(([^)]*)\)", location_text)
    return (match.group(1) if match else location_text).strip() or None


def _input_addresses(request: TimelineDraftRequest) -> dict[str, list[tuple[str, str]]]:
    """rawId → (문장에서 찾을 문자열, 그것이 나온 입력 주소 전체) 목록."""

    addresses: dict[str, list[tuple[str, str]]] = {}

    def add(raw_id: str | None, *values: str | None) -> None:
        if not raw_id:
            return
        found = [
            (variant, value)
            for value in values
            if value
            for variant in _variants(value)
            if len(variant) >= _MIN_ADDRESS_LENGTH
        ]
        if found:
            addresses.setdefault(raw_id, []).extend(found)

    for stay in request.stays:
        add(raw_id_of(stay), stay.address)
    for movement in request.movements:
        add(raw_id_of(movement), movement.end.address if movement.end else None)
    for photo in request.photos:
        add(raw_id_of(photo), photo.address)
    for calendar in request.calendars:
        add(raw_id_of(calendar), _calendar_address(calendar.location_text))
    return addresses


def _district(address: str) -> str | None:
    """주소의 가장 좁은 시·군·구. `서울특별시 예시구 예시로 123` → `예시구`."""

    tokens = [token for token in address.split() if _DISTRICT.match(token)]
    return tokens[-1] if tokens else None


def _looks_like_address(text: str) -> bool:
    return any(shape.search(text) for shape in _ADDRESS_SHAPES)


def _replacement(event: TimelineEventDraft, address: str) -> str | None:
    place = (event.place or "").strip()
    if place and not is_vague_place_label(place) and place not in address:
        if not _looks_like_address(place):
            return place
    return _district(address)


def replace_input_addresses(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """제목·본문의 입력 주소 원문을 장소명이나 시·군·구로 바꾼다(in-place)."""

    addresses = _input_addresses(request)
    if not addresses:
        return

    replaced = 0
    for event in draft.events:
        # 긴 주소부터 바꾼다. `… 123-4` 를 바꾸기 전에 `… 123` 이 먼저 바뀌면 `-4` 가 남는다.
        candidates = sorted(
            {
                pair
                for ref in event.source_refs
                for pair in addresses.get(ref.raw_id, [])
            },
            key=lambda pair: (-len(pair[0]), pair),
        )
        for address, original in candidates:
            if address not in event.title and address not in event.description:
                continue
            substitute = _replacement(event, original)
            if not substitute:
                continue  # 바꿀 이름이 없다. 문장 검사가 짚어 Repair 가 고친다
            event.title = event.title.replace(address, substitute).strip() or substitute
            event.description = event.description.replace(address, substitute)
            replaced += 1

    if replaced:
        logger.debug("문장의 입력 주소 원문 %d건을 장소명으로 바꿨습니다.", replaced)


@dataclass(frozen=True)
class NarrativeFinding:
    kind: str
    event: TimelineEventDraft
    found: tuple[str, ...]

    def detail(self) -> dict:
        return {"found": list(self.found)}


def find_narrative_labels(draft: TimelineDraft) -> list[NarrativeFinding]:
    """고치지 않고 찾는다: 제목의 `체류`, 제목의 시각, 문장의 주소 모양."""

    findings: list[NarrativeFinding] = []
    for event in draft.events:
        if stay := _STAY_WORD.findall(event.title):
            findings.append(NarrativeFinding("STAY_WORD_IN_TITLE", event, tuple(stay)))
        if times := [match.group(0) for match in _TIME_IN_TITLE.finditer(event.title)]:
            findings.append(NarrativeFinding("TIME_IN_TITLE", event, tuple(times)))
        shapes = [
            match.group(0)
            for text in (event.title, event.description)
            for shape in _ADDRESS_SHAPES
            for match in shape.finditer(text)
        ]
        if shapes:
            findings.append(
                NarrativeFinding("ADDRESS_IN_NARRATION", event, tuple(dict.fromkeys(shapes)))
            )
    return findings
