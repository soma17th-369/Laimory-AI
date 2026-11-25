"""기본 event (#167·#169·#202, v4 세트).

**하루의 event 는 코드가 정한다**(#202). 기본 event(`E1`, `E2` …)가 그날의 event 이고, Timeline 과
Repair 는 기본 event 마다 종류와 글(제목·문장·장소 후보 순서·알림 근거)만 쓴다. 시간과 위치 기록·사진·일정
근거는 기본 event 가 갖는다. 모델이 event 를 합치든, 사진마다 쪼개든, 빠뜨리든 구조가 같다 — 예전에는
Timeline 이 기본 event 위에서 사진·결제를 보고 event 를 꺼내고 경계를 옮겨, 같은 카페의 입구·내부 사진이
실행마다 다른 event 로 갈라졌다.

기본 event 는 다섯 종류다(`BaseEventKind`). 무엇이 기본 event 가 되는지는 `clue_bundles` 가 정한다.

- 위치 뼈대의 구간(체류·산책·이동)마다 하나. 구간을 덮는 일정은 그 체류의 일정이 된다(`with_calendar`).
- 방문을 다 덮지 않는 시각 있는 일정마다 하나(긴 방문 안의 회의, 방문이 없는 시간의 일정, 배경 일정).
- 어느 방문·일정에도 들지 않은 사진 장면마다 하나.

틀 문장은 「문장 형식」 표의 뼈대와 같다. 활동을 말해 주는 단서가 없는 구간은 이 문장이 그대로
맞는 최종 문장이고, Timeline 이 기본 event 를 빠뜨리면 이 글로 남는다. 일정·사진을 가진 기본 event 는
틀 글로는 그 내용이 담기지 않아 Repair 에 맡긴다(`needs_writing`).

제목은 Timeline 이 쓴다. 코드는 제목에 쓸 **지명**(`area`)만 골라 입력에 싣는다(사용자 결정
2026-10-29). 같은 장소(250m) 묶음마다 하나이고, 시군구 + 읍면동이다(`마포구 도화동`). 읍면동이
없는 주소는 시군구 + 도로명이고(`기흥구 동백죽전대로`), 도로명 뒤 건물번호는 빼되 도로명 안의
숫자는 둔다(`여수시 통제영3길`). 구가 없는 시는 시를 구 자리로 본다. 시·도는 넓어서 붙이지 않고,
그날 시·도가 둘 이상일 때만 짧게 앞에 붙인다(`서울 마포구 도화동`).

기본 event 의 제목은 Timeline 이 글을 쓰지 않은 기본 event 에만 남는 틀이다(`{지명}에서 시간을 보냄`). v4 제목은
전부 음슴체로 끝난다(사용자 결정 2026-11-01, #179).

**일정은 Timeline 보다 먼저 기본 event 에 넣는다**(사용자 결정 2026-10-30 — "캘린더의 경우 Timeline이
처리하기 전에 Location 의 결과물에다가 박아넣어버려"). 일정이 체류 구간을 덮으면 그 구간의 기본 event 는
일정 기본 event 다(`with_calendar`) — 일정이 주인이고 체류는 그 일정의 위치 근거다. **시간은 위치 기록을
따른다**(#202, "방문의 경계는 위치 기록을 따른다"). 예전에는 일정 시간이었다. eventType
`CALENDAR_EVENT` 는 아직 타입을 고르지 않았다는 표시이고 일정 내용으로 Timeline·Repair 가 고른다(#184).
이동은 제목과 문장 모두 출발·도착 지명으로 쓴다(`도화동에서 역삼동으로 이동함` / `…이동했다.`). 이동도
제목과 문장은 Timeline 이 쓰고, 이 틀은 Timeline 입력에 실리고 글이 빠진 이동에 남는다(사용자
결정 2026-10-30 — 제목은 Timeline, 문장은 코드가 따로 쓰자 둘의 출발지가 갈렸다).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineEventDraft,
)
from app.schemas.clues import PhotoClue
from app.services.calendar_clues import CalendarClue
from app.services.location_skeleton import LocationRecord, SegmentKind, SkeletonSegment

#: 기본 event 근거의 `sourceRefs.reason`.
SEGMENT_REASON = "위치 구간"
#: 방문 안의 일정 event 가 함께 갖는 그 방문의 체류 기록. 장소 후보를 고르는 근거다.
PLACE_REASON = "같은 시간의 방문"
_CALENDAR_REASON = "일정"
_PHOTO_REASON = "사진"

#: `merge_reason` 표지. 코드가 만들었거나 구조를 바꿔 Repair 가 글을 써야 하는 event 다.
NEEDS_WRITING = "코드가 구조를 정했고 제목·문장은 Repair 가 근거로 쓴다."

#: 사진 내용을 모를 때 사진 기본 event 의 제목.
_PHOTO_TITLE = "사진을 남김"
_TITLE_MAX = 30


class BaseEventKind(StrEnum):
    """기본 event 가 어디서 왔는지."""

    STAY = "STAY"
    WALK = "WALK"
    MOVEMENT = "MOVEMENT"
    #: 방문을 다 덮지 않는 일정. 방문을 덮는 일정은 그 체류의 기본 event 에 들어가 `STAY` 다.
    CALENDAR = "CALENDAR"
    #: 어느 방문·일정에도 들지 않은 사진 장면.
    PHOTO = "PHOTO"


#: 구간의 종류가 정하는 eventType. 이동은 이동이고 산책은 운동이다 — 모델이 바꾸지 않는다.
_FIXED_TYPES = {SegmentKind.MOVEMENT: EventType.MOVEMENT, SegmentKind.WALK: EventType.EXERCISE}

#: 시·도의 짧은 이름. 주소의 첫 어절로 읽는다. 표에 없는 시·도는 끝의 행정 단위를 뗀다.
_SIDO_SHORT = {
    "서울특별시": "서울", "서울": "서울",
    "부산광역시": "부산", "부산": "부산",
    "대구광역시": "대구", "대구": "대구",
    "인천광역시": "인천", "인천": "인천",
    "광주광역시": "광주", "광주": "광주",
    "대전광역시": "대전", "대전": "대전",
    "울산광역시": "울산", "울산": "울산",
    "세종특별자치시": "세종", "세종": "세종",
    "경기도": "경기", "경기": "경기",
    "강원특별자치도": "강원", "강원도": "강원", "강원": "강원",
    "충청북도": "충북", "충북": "충북",
    "충청남도": "충남", "충남": "충남",
    "전북특별자치도": "전북", "전라북도": "전북", "전북": "전북",
    "전라남도": "전남", "전남": "전남",
    "경상북도": "경북", "경북": "경북",
    "경상남도": "경남", "경남": "경남",
    "제주특별자치도": "제주", "제주도": "제주", "제주": "제주",
}
_SIDO_SUFFIX = re.compile(r"(통합특별시|특별자치시|특별자치도|특별시|광역시|도)$")
_GU = re.compile(r"^[가-힣]+구$")
_SI_GUN = re.compile(r"^[가-힣]+(시|군)$")
_EUP_MYEON_DONG = re.compile(r"^[가-힣][가-힣0-9.·]*(동|리|가|읍|면)$")
_ROAD = re.compile(r"^[가-힣][가-힣0-9]*(로|길)$")


def sido_of(address: str | None) -> str | None:
    """주소의 시·도 짧은 이름. 첫 어절이 시·도가 아니면 None 이다."""

    tokens = address.split() if address else []
    if not tokens:
        return None
    first = tokens[0]
    if first in _SIDO_SHORT:
        return _SIDO_SHORT[first]
    if _SIDO_SUFFIX.search(first):
        return _SIDO_SUFFIX.sub("", first) or None
    return None


@dataclass(frozen=True)
class AreaNamer:
    """제목에 쓰는 지명. 그날 시·도가 둘 이상이면 시·도를 앞에 붙인다."""

    multi_region: bool = False

    def name(self, address: str | None) -> str | None:
        tokens = address.split() if address else []
        if not tokens:
            return None
        rest = _before_number(tokens[1:] if sido_of(address) else tokens)
        district = next((t for t in rest if _GU.match(t)), None) or next(
            (t for t in rest if _SI_GUN.match(t)), None
        )
        # 읍면동이 먼저고, 없으면 도로명이다. 앞에서부터 찾아 `오송읍 봉산리` 는 읍이다 — 구 + 동과
        # 같은 단위라서다.
        local = next((t for t in rest if _EUP_MYEON_DONG.match(t)), None) or next(
            (t for t in rest if _ROAD.match(t)), None
        )
        parts = [part for part in (district, local) if part]
        if not parts:
            return None
        sido = sido_of(address) if self.multi_region else None
        return " ".join([sido, *parts] if sido else parts)


def district_of(address: str | None) -> tuple[str | None, str] | None:
    """주소의 시·도(짧은 이름)와 시군구. 시군구가 없으면 None 이다.

    시·도를 함께 둔다 — `중구` 는 서울에도 대구에도 있다. 주소에 시·도가 없으면 그 자리는 None 이다.
    """

    tokens = address.split() if address else []
    rest = _before_number(tokens[1:] if sido_of(address) else tokens)
    district = next((t for t in rest if _GU.match(t)), None) or next(
        (t for t in rest if _SI_GUN.match(t)), None
    )
    return (sido_of(address), district) if district else None


def _before_number(tokens: list[str]) -> list[str]:
    """번지 앞의 어절. 뒤는 번지·건물명이라 `101동`·`우성동` 이 지명으로 읽히면 안 된다."""

    for position, token in enumerate(tokens):
        if token[0].isdigit() or token == "산" or token.startswith("("):
            return tokens[:position]
    return tokens


def area_namer(addresses: Iterable[str | None]) -> AreaNamer:
    regions = {sido for address in addresses if (sido := sido_of(address))}
    return AreaNamer(multi_region=len(regions) > 1)


#: 숫자로 끝나는 말의 마지막 숫자를 읽을 때 받침. 0(영)·3(삼)·6(육)은 받침이 있고,
#: 1(일)·7(칠)·8(팔)은 ㄹ 받침이라 `로` 를 쓴다.
_DIGIT_HAS_FINAL = {"0": True, "3": True, "6": True}


def toward_particle(word: str) -> str:
    """방향 조사 `으로`/`로`. ㄹ 받침과 받침 없는 말은 `로` 다."""

    last = word.rstrip()[-1:] if word else ""
    if last.isdigit():
        return "으로" if _DIGIT_HAS_FINAL.get(last, False) else "로"
    if "가" <= last <= "힣":
        final = (ord(last) - ord("가")) % 28
        return "로" if final in (0, 8) else "으로"
    return "로"


def movement_route(origin: str | None, destination: str | None) -> str:
    """이동의 출발·도착 지명 구절(`마포구 도화동에서 강남구 역삼동으로`). 같은 지명이면 도착만 쓴다."""

    if origin == destination:
        origin = None
    parts = [f"{origin}에서"] if origin else []
    if destination:
        parts.append(f"{destination}{toward_particle(destination)}")
    return " ".join(parts)


@dataclass(frozen=True)
class BaseEvent:
    """하루의 event 하나. 시간과 위치 기록·사진·일정 근거는 코드가 정한 값이다(#202)."""

    base_id: str
    #: 위치 구간. 일정·사진 기본 event 에는 없다.
    segment: SkeletonSegment | None
    event_type: EventType
    title: str
    description: str
    #: 이 기본 event 의 일정. 위치 구간이 있으면 그 구간을 덮은 일정이고, 없으면 이 event 자체가 일정이다.
    calendar: CalendarClue | None = None
    #: 이 event 에 든 사진. 사진은 기본 event 하나에만 있다.
    photos: tuple[PhotoClue, ...] = ()
    #: 방문 안의 일정 event 가 함께 갖는 그 방문의 체류 기록. 장소 후보를 고르는 근거다.
    place_records: tuple[LocationRecord, ...] = ()
    #: 이 event 를 감싸는 방문의 기본 event id. 방문 안의 일정·사진 event 에만 있다.
    inside: str | None = None

    @property
    def kind(self) -> BaseEventKind:
        if self.segment is not None:
            return BaseEventKind(self.segment.kind.value)
        return BaseEventKind.CALENDAR if self.calendar is not None else BaseEventKind.PHOTO

    @property
    def start(self) -> datetime:
        if self.segment is not None:
            return self.segment.start
        if self.calendar is not None and self.calendar.start is not None:
            return self.calendar.start
        return min(photo.taken_at for photo in self.photos if photo.taken_at is not None)

    @property
    def end(self) -> datetime:
        if self.segment is not None:
            return self.segment.end
        if self.calendar is not None and self.calendar.end is not None:
            return self.calendar.end
        return max(photo.taken_at for photo in self.photos if photo.taken_at is not None)

    @property
    def fixed_type(self) -> EventType | None:
        """구간의 종류가 정하는 eventType(이동·산책). 그 밖에는 Timeline 이 고른다."""

        return _FIXED_TYPES.get(self.segment.kind) if self.segment is not None else None

    @property
    def all_day(self) -> bool:
        """캘린더 원본에서 종일로 표시된 일정의 event 인가. 하루 맨 앞에 둔다(#202)."""

        return self.segment is None and self.calendar is not None and self.calendar.all_day

    @property
    def needs_writing(self) -> bool:
        """틀 글로는 내용이 담기지 않는가. 일정·사진은 그 내용을 글이 말해야 한다."""

        return self.calendar is not None or bool(self.photos)

    def as_prompt_dict(self) -> dict[str, object]:
        # 제목은 싣지 않는다. Timeline 이 근거의 지명으로 쓴다.
        # 일정·사진이 있으면 틀 문장도 싣지 않는다(#202) — 보여 주자 Timeline 이 사진 있는 방문에
        # `{place}에서 시간을 보냈다.` 를 그대로 옮겼다(같은 날 trace 사진 있는 방문 20칸 중 7칸).
        payload: dict[str, object] = {"eventType": self.event_type.value}
        if self.description and not self.needs_writing:
            payload["description"] = self.description
        return payload

    def photo_mismatch(self, cited: list[str]) -> tuple[list[str], list[str]]:
        """글이 근거로 적은 사진 id 를 이 event 의 사진과 맞춘다(#202).

        (이 event 의 사진이 아닌 id, 적지 않은 이 event 의 사진) 이다. 사진은 코드가 event 마다 정했으니
        그 배정이 정답표다 — 글이 옆 event 의 사진을 읽어 썼으면 id 에서 드러난다.
        """

        own = [photo.clue_id for photo in self.photos]
        foreign = list(dict.fromkeys(identifier for identifier in cited if identifier not in own))
        missing = [identifier for identifier in own if identifier not in cited]
        return foreign, missing


def build_base_event(
    segment: SkeletonSegment,
    number: int,
    area: str | None = None,
    route: tuple[str | None, str | None] = (None, None),
) -> BaseEvent:
    """구간의 기본 event. `area` 는 그 구간 장소의 지명이다(되살릴 때 쓰는 틀 제목에만 쓴다).

    `route` 는 이동의 출발·도착 지명이다.
    """

    if segment.kind is SegmentKind.STAY:
        return BaseEvent(f"E{number}", segment, EventType.STAY, stay_title(area), "{place}에서 시간을 보냈다.")
    if segment.kind is SegmentKind.WALK:
        return BaseEvent(
            f"E{number}", segment, EventType.EXERCISE, f"{area}에서 산책함" if area else "산책함",
            "{place}에서 산책했다.",
        )
    phrase = movement_route(*route)
    return BaseEvent(
        f"E{number}", segment, EventType.MOVEMENT,
        f"{phrase} 이동함" if phrase else "이동함", f"{phrase} 이동했다." if phrase else "이동했다.",
    )


def stay_title(area: str | None) -> str:
    """활동을 모르는 체류의 제목. 지명이 없으면 지명 없이 쓴다."""

    return f"{area}에서 시간을 보냄" if area else "시간을 보냄"


def with_calendar(base: BaseEvent, calendar: CalendarClue) -> BaseEvent:
    """일정이 덮은 체류의 기본 event. 일정이 주인이고 체류는 그 위치 근거다(사용자 결정 2026-10-30).

    제목과 문장은 「문장 형식」의 일정 뼈대다. 일정이 활동을 말하므로 제목에 지명을 붙이지 않고(#179),
    참석했는지는 모르므로 `일정이 있었음`·`일정이 있었다` 로 둔다. 시간은 체류 구간이다(#202).
    """

    return BaseEvent(
        base.base_id,
        base.segment,
        EventType.CALENDAR_EVENT,
        f"{calendar.title} 일정이 있었음",
        f"{{place}}에서 {calendar.title} 일정이 있었다.",
        calendar,
    )


def calendar_base_event(
    calendar: CalendarClue,
    *,
    place_records: Iterable[LocationRecord] = (),
    inside: str | None = None,
) -> BaseEvent:
    """방문을 다 덮지 않는 일정의 기본 event. 시간은 일정 시간이다.

    문장은 비워 둔다 — 일정이 무엇을 하는 자리인지는 Timeline 이 쓰고, 틀 문장(`캘린더에 적어 둔 일정이다`)은
    일정의 내용을 묻었다(사용자 결정 2026-10-31).
    """

    return BaseEvent(
        "",
        None,
        EventType.CALENDAR_EVENT,
        f"{calendar.title} 일정이 있었음"[:_TITLE_MAX],
        "",
        calendar,
        place_records=tuple(place_records),
        inside=inside,
    )


def photo_base_event(photos: Iterable[PhotoClue], *, inside: str | None = None) -> BaseEvent:
    """어느 방문·일정에도 들지 않은 사진 장면의 기본 event. 시간은 첫 촬영부터 마지막 촬영까지다.

    글은 사진 단서의 말을 옮겨 둔다. `사진을 남겼다` 같은 고정 문장은 사진의 내용을 묻었다(사용자 결정
    2026-10-31).
    """

    photos = tuple(photos)
    told = next(((p.activity or p.description or "").strip(" .") for p in photos if p.activity or p.description), "")
    return BaseEvent(
        "",
        None,
        EventType.PHOTO_MOMENT,
        told[:_TITLE_MAX] or _PHOTO_TITLE,
        f"{told}." if told else "",
        photos=photos,
        inside=inside,
    )


def _refs(base: BaseEvent) -> list[SourceRef]:
    refs = [
        SourceRef(
            source_type=EventSourceType.STAY if record.is_stay else EventSourceType.MOVEMENT,
            raw_id=record.raw_id,
            reason=SEGMENT_REASON,
        )
        for record in (base.segment.records if base.segment is not None else ())
    ]
    held = {ref.raw_id for ref in refs}
    refs.extend(
        SourceRef(source_type=EventSourceType.STAY, raw_id=record.raw_id, reason=PLACE_REASON)
        for record in base.place_records
        if record.raw_id not in held
    )
    if base.calendar is not None:
        refs.append(
            SourceRef(
                source_type=EventSourceType.CALENDAR, raw_id=base.calendar.raw_id, reason=_CALENDAR_REASON
            )
        )
    refs.extend(
        SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id, reason=_PHOTO_REASON)
        for photo in base.photos
    )
    return refs


def structural_refs(base: BaseEvent) -> list[SourceRef]:
    """기본 event 의 위치 기록·일정·사진 근거. 코드가 정한 값이라 모델이 바꾸지 않는다(#202)."""

    return _refs(base)


def base_event_draft(
    base: BaseEvent,
    *,
    places: list[str] | None = None,
    client_event_id: str | None = None,
) -> TimelineEventDraft:
    """기본 event 를 틀 글 그대로 draft event 로 만든다. 시간과 근거는 기본 event 의 것이다.

    일정·사진을 가진 기본 event 는 틀 글로 내용이 담기지 않아 `NEEDS_WRITING` 으로 Repair 에 맡긴다.
    """

    return TimelineEventDraft(
        client_event_id=client_event_id or f"base-{base.base_id}",
        event_type=base.event_type,
        title=base.title,
        description=base.description,
        start_time=base.start,
        end_time=base.end,
        confidence=0.5,
        inference_level=InferenceLevel.DIRECT,
        source_refs=_refs(base),
        place_candidates=[] if base.event_type is EventType.MOVEMENT else list((places or [])[:4]),
        merge_reason=(
            NEEDS_WRITING
            if base.needs_writing
            else "Timeline 이 쓰지 않은 기본 event 를 코드가 틀 문장으로 남겼다."
        ),
        base_event_ids=[base.base_id],
    )
