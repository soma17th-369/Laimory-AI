"""장소 칸 (#171, v4 세트).

v4 문장은 장소 자리를 `{place}` 칸으로 남긴다. 칸에 들어갈 이름은 근거로 코드가 정하거나,
애매하면 후보 목록으로 내보내 사용자가 고른다(App Server 공지 2026-10-29). 확정 pass 의 두
자리에서 돈다.

1. **장소 확정**(`confirm_place_slots`) — v3 의 `resolve_places` 자리. event 마다 장소 후보
   풀을 만들고, 다른 근거가 풀의 이름을 가리키면 확정하고, 아니면 Timeline 이 고른 후보를
   풀 안의 것만 남긴다.
2. **문장 장소 칸**(`fill_place_slots`) — v3 의 `문장 주소 정리` 자리. event 를 지우거나
   합치는 단계가 끝난 뒤 문장을 맞춘다. 이동 제목·문장의 장소명과 주소 원문을 고치고, 칸을 쓸 수
   없는 event 의 칸을 주소로 바꾼다.

규칙(사용자 결정 2026-10-29).

- **후보**: 장소 이름은 언제나 후보 목록이다(사용자 결정, 최대 5개). 결제·예약 알림의 가맹점이나
  일정 장소명처럼 근거가 가리키는 이름이 1번이고, 그다음이 Timeline 이 풀에서 활동에 맞게 고른 순서,
  그다음이 풀의 나머지와 같은 장소(250m)의 이름이다. 풀 밖의 이름과 시설은 버린다. `place` 는
  1번 후보다 — App Server 도 그렇게 저장한다.
- **주소 대체**: 근거에 장소 이름이 하나도 없을 때만 `{place}` 를 대표 지점의 주소 원문으로 바꾼다. 동네 이름으로
  바꾸지 않는다. 주소도 없으면 칸과 그 조사를 지운다 — 칸이 남은 채 장소가 없으면 App Server
  가 하루치 결과 전체를 400 으로 거절한다.
- **이동**: 장소명을 쓰지 않고 후보도 없다. 제목과 문장은 Timeline·Repair 가 같은 출발·도착
  지명(구 + 동)으로 쓴다(사용자 결정 2026-10-30 — 제목은 Timeline, 문장은 코드가 따로 쓰자 둘의
  출발지가 갈렸다). 출발·도착 지명은 기록이 정하는 값이라 코드가 맞춘다 — 글에 기록의 경로가 없으면
  경로를 다시 쓰고 LLM 이 고른 목적(`출근`·`이동했다.`)은 둔다. 장소명은 뺀다. 제목과 문장에 같은
  규칙을 써서 둘이 갈라지지 않는다.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import tzinfo

from app.core.logging import get_logger
from app.schemas import (
    CalendarItem,
    EventType,
    MovementItem,
    PhotoItem,
    StayItem,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
)
from app.schemas.clues import NotificationClueKind
from app.schemas.common import PlacePoint
from app.schemas.timeline_result import MAX_PLACE_CANDIDATES, PLACE_SLOT
from app.services.base_events import AreaNamer, area_namer, movement_route, toward_particle
from app.services.calendar_clues import split_location_text
from app.services.clue_bundles import TimelineClues
from app.services.place_resolver import (
    is_exact_address,
    same_place_addresses,
    stays_by_overlap,
    touches_event,
    unique_place_names,
)
from app.services.place_text import normalize_place_text, place_text_contains, place_tokens
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)


#: 칸 뒤에 붙일 수 있는 조사(승인 결정 1). 칸을 지울 때 함께 지운다.
_SLOT_WITH_PARTICLE = re.compile(r"\{place\}(?:에서|에|의|까지)?\s*")

#: 머문 곳이 될 수 없는 시설. 역지오코딩 후보에 자주 섞여 오고 Timeline 이 1번 후보로 고르기도
#: 했다(`포스트타워마포 주차장`). 프롬프트 규칙과 같은 목록이고, 어떤 event 의 후보도 되지 않는다.
_FACILITY_NAME = re.compile(r"(주차장|대여소|정류장|정류소)$")


@dataclass(frozen=True)
class _Evidence:
    stays: list[StayItem]
    movements: list[MovementItem]
    photos: list[PhotoItem]
    calendars: list[CalendarItem]
    notification_ids: set[str]


def without_place_slot(description: str, area: str | None) -> str:
    """장소 이름이 없을 때의 문장. 칸을 지명으로 바꾸고, 지명도 없으면 칸과 조사를 지운다.

    주소를 통째로 넣지 않는다(사용자 결정 2026-10-31) — `대구 북구 산격동 1346에서` 가 아니라 `북구 산격동에서` 다.
    """

    if area:
        return description.replace(PLACE_SLOT, area)
    return _SLOT_WITH_PARTICLE.sub("", description).strip()


def filled_description(event: TimelineEventDraft) -> str:
    """칸을 채운 문장. 문장 길이 검사와 Question 입력이 본다(v3 문장은 그대로 돌려준다)."""

    text = event.description or ""
    if PLACE_SLOT not in text:
        return text
    return text.replace(PLACE_SLOT, (event.place or AreaNamer().name(event.address) or "").strip())


def confirm_place_slots(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues | None
) -> None:
    """event 마다 장소 후보를 정한다(in-place). 1번 후보가 `place` 다."""

    tz = resolve_timezone(request.timezone)
    index = _Index.build(request)
    same_place = same_place_addresses(request, tz)
    names_by_place = _names_by_place(request, same_place, tz)
    confirmed = candidates = addressed = 0
    for event in draft.events:
        evidence = index.evidence(event, tz)
        if event.event_type is EventType.MOVEMENT:
            event.place = None
            event.place_candidates = []
            event.address = _same(same_place, _arrival_address(evidence.movements, tz))
            continue

        pool = _with_same_place(_pool(evidence, tz), evidence, same_place, names_by_place)
        # 장소명은 언제나 후보 목록으로 준다(사용자 결정). 결제·예약 가맹점이나 일정 장소처럼
        # 근거가 가리키는 이름은 확정해서 후보를 지우지 않고 1순위에 둔다. 그다음이 Timeline 이
        # 고른 순서, 그다음이 고르지 않은 근거의 이름이다 — 위치 근거만 있어 무엇을 했는지 모르는
        # 체류도 어디였는지는 후보로 묻는다. 칸을 주소로 채우는 것은 이름이 하나도 없을 때뿐이다.
        pointed = _confirmed_name(evidence, pool, clues)
        keys = {normalize_place_text(item): item for item in pool}
        ranked = [
            keys[normalize_place_text(item)]
            for item in event.place_candidates
            if normalize_place_text(item) in keys
        ]
        kept = unique_place_names([pointed, *ranked, *pool])[:MAX_PLACE_CANDIDATES]
        event.place_candidates = kept
        event.place = kept[0] if kept else None
        confirmed += pointed is not None
        candidates += bool(kept)
        # 같은 장소는 하루 안에서 한 주소다 — 오전·오후의 같은 사무실이 다른 주소로 나가지 않게 한다.
        event.address = _same(same_place, _representative_address(evidence, event.place, tz)) or (
            _calendar_address(evidence) if not pool else None
        )
        addressed += event.address is not None

    logger.debug(
        "장소 칸 확정: confirmed=%d, candidates=%d, address=%d", confirmed, candidates, addressed
    )


def fill_place_slots(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    clues: TimelineClues | None = None,
    *,
    slots_only: bool = False,
) -> None:
    """문장을 장소 칸 계약에 맞춘다(in-place). event 를 지우거나 합치는 단계 뒤에 돈다."""

    tz = resolve_timezone(request.timezone)
    index = _Index.build(request)
    same_place = same_place_addresses(request, tz)
    names_by_place = _names_by_place(request, same_place, tz)
    raw_addresses = _day_raw_addresses(request)
    day_addresses = _address_forms(raw_addresses)
    areas = clues.areas if clues else AreaNamer()
    movement_names = _place_names_outside_areas(request, raw_addresses, same_place, areas)
    for event in draft.events:
        if slots_only and event.event_type is EventType.MOVEMENT:
            continue
        evidence = index.evidence(event, tz)
        if event.event_type is EventType.MOVEMENT:
            route = _movement_route(evidence.movements, tz, same_place, areas)
            event.title = _movement_text(event.title, route, movement_names, "이동")
            event.description = _movement_text(event.description, route, movement_names, "이동했다.")
            continue

        names = unique_place_names(
            [
                event.place,
                *event.place_candidates,
                *_with_same_place(_pool(evidence, tz), evidence, same_place, names_by_place),
            ]
        )
        description = event.description or ""
        # 장소 이름이나 입력 주소를 직접 쓴 문장은 칸으로 바꾼다. 긴 것부터 바꿔 부분 일치가 남지
        # 않게 한다. 주소는 후보가 없어도 바꾼다 — 칸은 아래에서 다시 그 주소로 채워진다.
        addresses = [
            address.strip()
            for address in (
                event.address,
                *(stay.address for stay in evidence.stays),
                *(photo.address for photo in evidence.photos),
            )
            if address and address.strip()
        ]
        # 번지를 뺀 주소(`전남광주통합특별시 여수시 여서동`)도 주소다 — Repair 가 그렇게 써서 칸이
        # 사라졌다. 그날 입력의 주소를 시·군·구 단위까지 줄여 가며 함께 찾는다.
        literals = [
            *addresses,
            *day_addresses,
            *(names if (event.place or event.place_candidates) else []),
        ]
        if PLACE_SLOT not in description:
            for literal in sorted(set(literals), key=len, reverse=True):
                if len(literal) >= 2 and literal in description:
                    description = description.replace(literal, PLACE_SLOT, 1)
                    break
        if not slots_only and PLACE_SLOT in description and not (event.place or event.place_candidates):
            description = without_place_slot(description, areas.name(_same(same_place, event.address)))
        event.description = description


def keep_place_names_out(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """제목과 이동 글에 장소 이름·칸을 남기지 않는다(in-place, v4, 사용자 결정 2026-10-31).

    "제목에는 절대 {place} 를 남기면 안되고 무조건 주소만 언급해야된다", "이동에서도 제목과 내용에 주소만
    나와야한다". Repair 가 놓치면 그대로 나갔다(제목 517개 중 1개). 장소 이름과 칸만 지명으로 바꾸거나 빼고
    나머지 글은 건드리지 않는다 — 코드가 글을 통째로 덮으면 일정·사진의 내용이 사라졌다.
    """

    tz = resolve_timezone(request.timezone)
    index = _Index.build(request)
    names = _place_names_outside_areas(
        request, _day_raw_addresses(request), clues.same_place, clues.areas
    )
    for event in draft.events:
        if event.event_type is EventType.MOVEMENT:
            movements = index.evidence(event, tz).movements
            route = _movement_route(movements, tz, clues.same_place, clues.areas)
            for field, default in (("title", "이동"), ("description", "이동했다.")):
                text = getattr(event, field) or ""
                if PLACE_SLOT in text or _without_place_names(text, names) != text:
                    cleaned = text.replace(PLACE_SLOT, "")
                    setattr(event, field, _movement_text(cleaned, route, names, default))
            continue
        event.title = _title_without_place(event.title, names, clues.area(event.address))


def _title_without_place(title: str, names: list[str], area: str | None) -> str:
    """제목의 칸과 장소 이름을 지명으로 바꾸거나 뺀다. 그 밖의 말은 그대로다."""

    text = title.replace(f"{PLACE_SLOT}에서 ", f"{area}에서 " if area else "")
    text = text.replace(PLACE_SLOT, area or "")
    if "에서 " in text:
        head, rest = text.split("에서 ", 1)
        if head.strip() in names and rest.strip():
            text = f"{area}에서 {rest.strip()}" if area else rest.strip()
    text = " ".join(_without_place_names(text, names).split())
    return text or area or title


def fix_counterpart_particles(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """대화 상대 이름 뒤의 `와`/`과` 를 이름의 받침에 맞춘다(in-place).

    「문장 형식」 표의 뼈대가 `{사람}와` 라서 Timeline 이 받침과 무관하게 `와` 를 옮겨 적는다
    (`욱희민영와 대화`). 프롬프트에 받침을 맞추라고 적어도 따르지 않았다. 대화 상대 이름은
    알림 단서가 알고 있어 코드가 맞춘다. 이름 끝의 이모지는 건너뛰고 마지막 한글로 읽는다.
    """

    names: set[str] = set()
    for clue in clues.notifications:
        if clue.counterpart and clue.counterpart.strip():
            names.add(clue.counterpart.strip())
            core = re.sub(r"[^0-9A-Za-z가-힣 ]", "", clue.counterpart).strip()
            if core:
                names.add(core)
    if not names:
        return
    for event in draft.events:
        event.title = _with_particles(event.title, names)
        event.description = _with_particles(event.description or "", names)


def _with_particles(text: str, names: set[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        last = next((ch for ch in reversed(name) if "가" <= ch <= "힣"), None)
        if last is None:
            continue
        particle = "와" if (ord(last) - ord("가")) % 28 == 0 else "과"
        text = re.sub(
            rf"(?<![가-힣]){re.escape(name)}(와|과)(?![가-힣])",
            lambda match: f"{name}{particle}",
            text,
        )
    return text


def fix_title_places(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """제목 앞 `{X}에서` 의 X 가 그 event 의 지명이 아니면 바로잡는다(in-place).

    제목에는 장소가 절대 들어가지 않는다(사용자 결정). 제목의 장소 자리는 코드가 고른 지명
    (`area`)뿐이다. 실제 Timeline 은 사진 해석의 `한식당에서` 를 옮겨 `한식당에서 저녁 식사` 를
    썼다(6회 중 4회). X 가 그 event 의 대표 주소 지명도, 그 시간에 머문 체류의 지명도 아니면
    대표 주소 지명으로 바꾸고, 대표 주소가 없으면 `{X}에서` 를 뺀다. 나머지 제목은 그대로다.
    """

    tz = resolve_timezone(request.timezone)
    for event in draft.events:
        if event.event_type is EventType.MOVEMENT or "에서 " not in event.title:
            continue
        head, rest = event.title.split("에서 ", 1)
        allowed = {clues.area(event.address)} | {
            clues.area(stay.address)
            for stay in request.stays
            if touches_event(stay, event, tz)
        }
        allowed.discard(None)
        if head.strip() in allowed or not rest.strip():
            continue
        right = clues.area(event.address)
        event.title = f"{right}에서 {rest.strip()}" if right else rest.strip()

    # 제목 어디에든 들어간 그날의 장소명을 뺀다. Timeline 은 위치·사진의 장소 후보나 사진 설명의
    # 상호를 제목 가운데에도 넣는다(`해녀밥상 점심`). 지명(`area`)은 장소명이 아니라 남긴다.
    # 이동 제목도 같다 — `동백호수공원 두산위브더제니스에서 도화동으로 이동` 이 나온 적이 있다.
    names = _place_names_outside_areas(
        request, _day_raw_addresses(request), clues.same_place, clues.areas
    )
    for event in draft.events:
        title = _without_place_names(event.title, names)
        # 가게 종류도 장소다(`한식당 식사`). 음식 종류가 붙은 말은 음식만 남기고(`한식당` → `한식`),
        # 장소만 말하는 말은 뺀다. 입력에 없는 말이라 위의 장소명 검사에 걸리지 않는다.
        title = _CUISINE_VENUE.sub(lambda m: f"{m.group(1)}식", title)
        title = _VENUE_WORD.sub("", title)
        title = " ".join(title.split())
        if title and title != event.title:
            event.title = title


#: 음식 종류가 붙은 가게 종류(`한식당`). 제목에서는 음식 종류만 남긴다.
_CUISINE_VENUE = re.compile(r"(한|중|일|양)식당")
#: 장소만 말하는 가게 종류. 제목에서 뺀다(뒤의 조사까지).
_VENUE_WORD = re.compile(
    r"(?:(?<=\s)|^)(?:식당|음식점|레스토랑|카페|커피숍|가게|편의점|마트|술집|주점|빵집|베이커리)"
    r"(?:에서|의|에)?(?=\s|$)\s*"
)


def _without_place_names(text: str, names: list[str]) -> str:
    """글에서 장소명을 뒤의 조사와 함께 뺀다. 어절 하나로 쓰인 이름만 본다."""

    for name in names:
        text = re.sub(rf"(?:(?<=\s)|^){re.escape(name)}(?:에서|으로|로|의|에)?(?=\s|$)\s*", "", text)
    return text


def _day_raw_addresses(request: TimelineDraftRequest) -> list[str | None]:
    return [
        *(stay.address for stay in request.stays),
        *(m.end.address for m in request.movements if m.end is not None),
        *(m.start.address for m in request.movements if m.start is not None),
        *(photo.address for photo in request.photos),
    ]


def _place_names_outside_areas(
    request: TimelineDraftRequest,
    raw_addresses: list[str | None],
    same_place: dict[str, str],
    areas: AreaNamer,
) -> list[str]:
    """그날의 장소명 중 지명의 어절과 겹치지 않는 것. 겹치는 이름을 빼면 지명이 반쯤 지워진다."""

    area_words = {
        word
        for address in raw_addresses
        if (area := areas.name(_same(same_place, address)))
        for word in area.split()
    }
    return [
        name
        for name in sorted(_day_place_names(request), key=len, reverse=True)
        if name not in area_words
    ]


def _movement_text(text: str | None, route: str, names: list[str], default: str) -> str:
    """이동 제목·문장. 출발·도착 지명은 기록이 정하므로 코드가 맞추고, 목적은 LLM 이 쓴 것을 둔다.

    제목과 문장은 `{경로} {목적}` / `{경로} {목적}했다.` 꼴이다(「문장 형식」). 장소명을 뺀 글에
    기록의 경로가 그대로 있으면 둔다. 없으면(앞 체류를 출발지로 쓴 것, 조사가 틀린 것, 주소 원문을
    옮긴 것) 마지막 어절을 목적으로 보고 경로를 다시 붙인다. 실제 Timeline 은 기록되지 않은 열차
    구간 앞의 체류를 출발지로 썼다(8번 중 1번).
    """

    words = " ".join(_without_place_names(text or "", names).split())
    if not route:
        return words or default
    if route in words:
        return words
    last = words.split()[-1] if words else default
    if last.endswith(("에서", "로")):
        last = default
    return f"{route} {last}"


def _day_place_names(request: TimelineDraftRequest) -> set[str]:
    """그날 입력에 있는 장소명(체류·이동 도착지·사진의 이름과 일정 장소). 두 글자 이상만 본다."""

    names = [
        *(name for stay in request.stays for name in (stay.place, *stay.places)),
        *(name for m in request.movements if m.end for name in (m.end.place, *m.end.places)),
        *(name for m in request.movements if m.start for name in (m.start.place, *m.start.places)),
        *(name for photo in request.photos for name in photo.places),
        *(split_location_text(c.location_text)[0] for c in request.calendars),
    ]
    return {name.strip() for name in names if name and len(name.strip()) >= 2}


@dataclass(frozen=True)
class DayPlaceWords:
    """Question 이 입력에서 빼고 결과에서 찾는 그날의 장소 말(#182, v4).

    입력에서 확인되는 말만 갖는다. User Memory·일정 제목 속 장소는 문장이라 코드가 가려낼 수
    없어 프롬프트가 막는다. 장소명은 통째로만 본다 — 어절로 쪼개면 `필라테스 스튜디오` 때문에
    `필라테스` 끝나고를 묻는 질문이 걸린다. 지명은 `대현동` 처럼 어절로도 쓰여 세 글자 이상
    어절을 함께 보고, 두 글자 어절(`이동`)은 `이동하는 동안` 에 걸리므로 보지 않는다.
    접미사(`~동`·`~로`·`~역`)로 찾지 않는다 — `퇴근길`·`여자친구`·`산책로` 가 장소가 된다.
    """

    words: tuple[str, ...]

    def removed_from(self, text: str) -> str:
        """글에서 장소 말을 뒤의 조사와 함께 뺀다. 사용자가 읽는 글이 아니라 프롬프트 입력에 쓴다."""

        return " ".join(_without_place_names(text, list(self.words)).split())

    def found_in(self, text: str) -> bool:
        compact = normalize_place_text(text)
        return any(normalize_place_text(word) in compact for word in self.words)


def day_place_words(
    request: TimelineDraftRequest, events: list[TimelineEventDraft]
) -> DayPlaceWords:
    """그날 입력과 event 에 있는 장소명·주소·지명. 지운 조각이 남지 않게 긴 말부터 둔다."""

    addresses = {
        address.strip()
        for address in (*_day_raw_addresses(request), *(event.address for event in events))
        if address and address.strip()
    }
    # 지명은 두 꼴로 쓰인다. 제목은 그날 시·도가 둘이면 시·도를 붙이고 칸을 채운 문장은 붙이지 않는다.
    areas = {
        area
        for namer in (AreaNamer(), area_namer(addresses))
        for address in addresses
        if (area := namer.name(address))
    }
    names = {
        name.strip()
        for event in events
        for name in (event.place, *event.place_candidates)
        if name and len(name.strip()) >= 2
    }
    area_words = {word for area in areas for word in place_tokens(area)}
    words = names | _day_place_names(request) | addresses | areas | area_words
    return DayPlaceWords(tuple(sorted(words, key=lambda word: (-len(word), word))))


def place_names_in_title(event: TimelineEventDraft, request: TimelineDraftRequest) -> list[str]:
    """제목에 쓰인 장소 이름. 제목에는 장소명을 쓰지 않는다(사용자 결정). 찾기만 한다."""

    tz = resolve_timezone(request.timezone)
    evidence = _Index.build(request).evidence(event, tz)
    names = unique_place_names([event.place, *event.place_candidates, *_pool(evidence, tz)])
    return [name for name in names if len(name) >= 2 and name in event.title]


# --- 근거 --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Index:
    stays: dict[str, StayItem]
    movements: dict[str, MovementItem]
    photos: dict[str, PhotoItem]
    calendars: dict[str, CalendarItem]
    notifications: set[str]

    @classmethod
    def build(cls, request: TimelineDraftRequest) -> "_Index":
        def by_id(items) -> dict:
            return {raw_id: item for item in items if (raw_id := raw_id_of(item))}

        return cls(
            stays=by_id(request.stays),
            movements=by_id(request.movements),
            photos=by_id(request.photos),
            calendars=by_id(request.calendars),
            notifications=set(by_id(request.notifications)),
        )

    def evidence(self, event: TimelineEventDraft, tz: tzinfo) -> _Evidence:
        """event 의 근거. 체류는 event 시간과 겹치는 것만, 많이 겹치는 순서다.

        나눈 event 는 같은 체류 구간의 기록을 함께 근거로 갖는다. 시간이 겹치지 않는 기록의
        이름·주소가 다른 event 의 장소가 되지 않게 한다.
        """

        refs = [ref.raw_id for ref in event.source_refs]
        stays = [self.stays[r] for r in refs if r in self.stays]
        return _Evidence(
            stays=stays_by_overlap([s for s in stays if touches_event(s, event, tz)], event, tz),
            movements=[self.movements[r] for r in refs if r in self.movements],
            photos=[self.photos[r] for r in refs if r in self.photos],
            calendars=[self.calendars[r] for r in refs if r in self.calendars],
            notification_ids={r for r in refs if r in self.notifications},
        )


def _pool(evidence: _Evidence, tz: tzinfo) -> list[str]:
    """장소 후보 풀: event 시간과 많이 겹친 체류의 이름부터, 이동 도착지, 사진 지점 순이다.

    주차장·대여소·정류장 같은 시설은 빼낸다(`_FACILITY_NAME`).
    """

    names: list[str | None] = [n for stay in evidence.stays for n in (stay.place, *stay.places)]
    for movement in evidence.movements:
        if movement.end is not None:
            names.extend((movement.end.place, *movement.end.places))
    names.extend(name for photo in evidence.photos for name in photo.places)
    return [name for name in unique_place_names(names) if not _FACILITY_NAME.search(name.strip())]


def _address_forms(addresses: list[str | None]) -> list[str]:
    """주소 원문과, 끝의 어절을 하나씩 뗀 앞부분(두 어절 이상). 문장에 직접 쓴 주소를 찾는 데 쓴다."""

    forms: set[str] = set()
    for address in addresses:
        tokens = (address or "").split()
        for end in range(len(tokens), 1, -1):
            forms.add(" ".join(tokens[:end]))
    return sorted(forms, key=len, reverse=True)


#: 입력에 실린 장소 이름 하나와 그 원소의 좌표. 좌표가 없는 이름(단수 `place`, 입력 조회 v1)은 None 이다.
_Label = tuple[str, PlacePoint | None]


def _labels(names: Iterable[str | None], points: list[PlacePoint]) -> list[_Label]:
    by_name: dict[str, PlacePoint] = {}
    for point in points:
        by_name.setdefault(point.name.strip(), point)
    return [(name.strip(), by_name.get(name.strip())) for name in names if name and name.strip()]


def _labels_by_place(
    request: TimelineDraftRequest, same_place: dict[str, str], tz: tzinfo
) -> dict[str, list[_Label]]:
    """같은 장소마다 그 장소의 지점들에서 나온 이름과 좌표. 오래 머문 체류의 이름이 앞이다."""

    entries: list[tuple[float, str | None, list[_Label]]] = []
    for stay in request.stays:
        start = parse_datetime(stay.start_at, tz)
        end = parse_datetime(stay.end_at, tz) if stay.end_at else start
        weight = (end - start).total_seconds() if start and end else 0.0
        entries.append((weight, stay.address, _labels([stay.place, *stay.places], stay.place_points)))
    for movement in request.movements:
        if (spot := movement.end) is not None:
            entries.append((0.0, spot.address, _labels([spot.place, *spot.places], spot.place_points)))
    entries.extend(
        (0.0, photo.address, _labels(photo.places, photo.place_points)) for photo in request.photos
    )

    table: dict[str, list[_Label]] = {}
    for _, address, labels in sorted(entries, key=lambda item: -item[0]):
        if address and (place := _same(same_place, address)):
            table.setdefault(place, []).extend(labels)
    return table


def _names_by_place(
    request: TimelineDraftRequest, same_place: dict[str, str], tz: tzinfo
) -> dict[str, list[str]]:
    """같은 장소마다 그 장소의 지점들에서 나온 이름."""

    return {
        place: unique_place_names(name for name, _ in labels)
        for place, labels in _labels_by_place(request, same_place, tz).items()
    }


@dataclass(frozen=True)
class CandidateSources:
    """event 의 후보가 입력의 어디에서 왔는지 찾는 표. 결과 저장 직전에 후보를 입력과 대조한다.

    후보 풀(`_pool`·`_with_same_place`)과 같은 자리를 같은 순서로 본다 — event 의 체류·이동 도착지·사진,
    그다음 같은 장소(250m)의 다른 기록, 그리고 일정 장소명이다. 같은 이름이 여러 기록에 있으면 앞선 기록의
    좌표를 쓴다. 건물명은 기록마다 관측 좌표가 달라서, event 를 대표하는 기록의 값이 맞다.
    """

    index: _Index
    same_place: dict[str, str]
    labels_by_place: dict[str, list[_Label]]
    tz: tzinfo
    areas: AreaNamer

    @classmethod
    def build(cls, request: TimelineDraftRequest) -> "CandidateSources":
        tz = resolve_timezone(request.timezone)
        same_place = same_place_addresses(request, tz)
        areas = area_namer(_day_raw_addresses(request))
        return cls(_Index.build(request), same_place, _labels_by_place(request, same_place, tz), tz, areas)

    def area_of(self, address: str | None) -> str | None:
        """그 주소의 지명(구 + 동). 제목과 같은 지명이고 번지가 없다."""

        return self.areas.name(_same(self.same_place, address))

    def of(self, event: TimelineEventDraft) -> dict[str, PlacePoint | None]:
        """그 event 가 후보로 쓸 수 있는 이름(정규화한 값) → 입력 원소의 좌표."""

        evidence = self.index.evidence(event, self.tz)
        ends = [m.end for m in evidence.movements if m.end is not None]
        labels = [
            *(l for s in evidence.stays for l in _labels([s.place, *s.places], s.place_points)),
            *(l for e in ends for l in _labels([e.place, *e.places], e.place_points)),
            *(l for p in evidence.photos for l in _labels(p.places, p.place_points)),
        ]
        addresses = [
            *(stay.address for stay in evidence.stays),
            *(end.address for end in ends),
            *(photo.address for photo in evidence.photos),
        ]
        for address in addresses:
            if address and (place := _same(self.same_place, address)):
                labels.extend(self.labels_by_place.get(place, []))
        labels.extend(
            (name, None)
            for calendar in evidence.calendars
            if (name := split_location_text(calendar.location_text)[0])
        )

        table: dict[str, PlacePoint | None] = {}
        for name, point in labels:
            key = normalize_place_text(name)
            if key and table.get(key) is None:
                table[key] = point
        return table


def _with_same_place(
    pool: list[str],
    evidence: _Evidence,
    same_place: dict[str, str],
    names_by_place: dict[str, list[str]],
) -> list[str]:
    """근거의 이름 뒤에 같은 장소(250m)의 이름을 붙인다.

    같은 장소면 후보도 같다(사용자 결정). 점심 사진에는 이름이 없고 이름은 같은 자리의 체류 기록에
    있었다. 사무실 오후 기록에도 이름이 없고 이름은 같은 사무실의 사진·오전 기록에 있었다. 근거의
    이름만 보면 이 event 들의 칸이 주소로 채워졌다.
    """

    addresses = [
        *(stay.address for stay in evidence.stays),
        *(m.end.address for m in evidence.movements if m.end is not None),
        *(photo.address for photo in evidence.photos),
    ]
    extra = [
        name
        for address in addresses
        if address and (place := _same(same_place, address))
        for name in names_by_place.get(place, [])
    ]
    return [
        name for name in unique_place_names([*pool, *extra]) if not _FACILITY_NAME.search(name.strip())
    ]


def _confirmed_name(
    evidence: _Evidence, pool: list[str], clues: TimelineClues | None
) -> str | None:
    """다른 근거가 가리키는 이름. 결제·예약 가맹점, 일정 장소명 순이다."""

    pointed: list[str] = []
    if clues is not None:
        for clue in clues.notifications:
            if clue.kind in (NotificationClueKind.PAYMENT, NotificationClueKind.RESERVATION) and (
                evidence.notification_ids & set(clue.raw_ids)
            ):
                if clue.counterpart:
                    pointed.append(clue.counterpart)
    calendar_names = [
        place
        for calendar in evidence.calendars
        if (place := split_location_text(calendar.location_text)[0])
    ]
    pointed.extend(calendar_names)

    for name in pointed:
        for candidate in pool:
            if _same_name(name, candidate):
                return candidate
    if not pool and calendar_names:
        # 일정만 있어 풀이 없으면 일정이 말하는 장소가 그대로 장소다.
        return calendar_names[0]
    return None


def _same_name(left: str, right: str) -> bool:
    return place_text_contains(left, right)


def _representative_address(evidence: _Evidence, place: str | None, tz: tzinfo) -> str | None:
    """장소와 같은 지점의 주소. 장소가 없으면 event 와 가장 많이 겹친 체류, 이동 도착지, 사진 순이다."""

    points: list[tuple[tuple[str, ...], str | None]] = [
        (tuple(n for n in (s.place, *s.places) if n), s.address) for s in evidence.stays
    ]
    points.extend(
        (tuple(n for n in (m.end.place, *m.end.places) if n), m.end.address)
        for m in evidence.movements
        if m.end is not None
    )
    points.extend((tuple(p.places), p.address) for p in evidence.photos)
    exact = [(names, address.strip()) for names, address in points if is_exact_address(address)]
    if place:
        key = normalize_place_text(place)
        for names, address in exact:
            if any(normalize_place_text(name) == key for name in names):
                return address
    return exact[0][1] if exact else None


def _same(table: dict[str, str], address: str | None) -> str | None:
    return table.get(address.strip(), address) if address else address


def _calendar_address(evidence: _Evidence) -> str | None:
    for calendar in evidence.calendars:
        address = split_location_text(calendar.location_text)[1]
        if address:
            return address
    return None


def _arrival_address(movements: list[MovementItem], tz: tzinfo) -> str | None:
    ordered = _ordered(movements, tz)
    if not ordered or ordered[-1].end is None:
        return None
    address = ordered[-1].end.address
    return address.strip() if is_exact_address(address) else None


def _movement_route(
    movements: list[MovementItem],
    tz: tzinfo,
    same_place: dict[str, str] | None = None,
    areas: AreaNamer | None = None,
) -> str:
    """이동 경로: 출발·도착의 지명(구 + 동)이다(사용자 결정). 장소명도 주소 원문도 넣지 않는다.

    주소 원문(`경기도 용인시 기흥구 동백죽전대로 455-11`)을 그대로 쓰면 시·도부터 번지까지 늘어져
    사용자가 읽는 문장이 아니다(사용자: 확실한 오류). 제목과 같은 지명을 쓰고, 주소는 같은 장소의
    주소로 맞춘 뒤 줄인다 — 도착지가 그다음 체류 event 와 같은 지명으로 보인다.
    """

    namer = areas or AreaNamer()
    ordered = _ordered(movements, tz)
    origin = ordered[0].start.address if ordered and ordered[0].start else None
    destination = ordered[-1].end.address if ordered and ordered[-1].end else None
    return movement_route(
        namer.name(_same(same_place or {}, origin)),
        namer.name(_same(same_place or {}, destination)),
    )


def _ordered(movements: list[MovementItem], tz: tzinfo) -> list[MovementItem]:
    def key(item: MovementItem) -> float:
        parsed = parse_datetime(item.start_at, tz)
        return parsed.timestamp() if parsed else 0.0

    return sorted(movements, key=key)
