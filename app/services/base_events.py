"""기본 event (#167·#169, v4 세트).

코드가 위치 뼈대의 구간마다 기본 event(`E1`, `E2` …)를 틀 문장과 함께 만든다. Timeline v4 는
이 구조 위에서 근거를 더하고 내용을 풍성하게 하며, 근거가 있으면 구조를 바꾼다(나누기, 이어진
체류 합치기, 경계 조정). 바꾼 event 는 어느 기본 event 에서 왔는지(`from`)를 적는다.

틀 문장은 「문장 형식」 표의 뼈대와 같다. 활동을 말해 주는 단서가 없는 구간은 이 문장이 그대로
맞는 최종 문장이고, Timeline 이 기본 event 를 빠뜨려도 이 문장으로 하루가 덮인다
(`skeleton_guard.ensure_skeleton_segments`).

제목은 Timeline 이 쓴다. 코드는 제목에 쓸 **지명**(`area`)만 골라 입력에 싣는다(사용자 결정
2026-10-29). 같은 장소(250m) 묶음마다 하나이고, 시군구 + 읍면동이다(`마포구 도화동`). 읍면동이
없는 주소는 시군구 + 도로명이고(`기흥구 동백죽전대로`), 도로명 뒤 건물번호는 빼되 도로명 안의
숫자는 둔다(`여수시 통제영3길`). 구가 없는 시는 시를 구 자리로 본다. 시·도는 넓어서 붙이지 않고,
그날 시·도가 둘 이상일 때만 짧게 앞에 붙인다(`서울 마포구 도화동`).

기본 event 의 제목은 Timeline 이 빠뜨린 구간을 되살릴 때만 쓰는 틀이다(`{지명}에서 시간을 보냄`). v4 제목은
전부 음슴체로 끝난다(사용자 결정 2026-11-01, #179).

**일정은 Timeline 보다 먼저 기본 event 에 넣는다**(사용자 결정 2026-10-30 — "캘린더의 경우 Timeline이
처리하기 전에 Location 의 결과물에다가 박아넣어버려"). 일정이 체류 구간을 덮으면 그 구간의 기본 event 는
일정 기본 event 다(`with_calendar`) — eventType 은 `CALENDAR_EVENT`, 시간은 일정 시간이고 체류는 그 일정의
위치 근거다. 일정은 중요한 일정이라 일정이 주인이고 위치가 근거다. 프롬프트로만 맡기면 Timeline 이 회의와
같은 체류의 `보낸 시간` 을 따로 썼다(4번 중 1번).
이동은 제목과 문장 모두 출발·도착 지명으로 쓴다(`도화동에서 역삼동으로 이동함` / `…이동했다.`). 이동도
제목과 문장은 Timeline 이 쓰고, 이 틀은 Timeline 입력에 실리고 빠진 이동을 되살릴 때 쓰인다(사용자
결정 2026-10-30 — 제목은 Timeline, 문장은 코드가 따로 쓰자 둘의 출발지가 갈렸다).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineEventDraft,
)
from app.services.calendar_clues import CalendarClue
from app.services.location_skeleton import SegmentKind, SkeletonSegment

#: 기본 event 근거의 `sourceRefs.reason`.
SEGMENT_REASON = "위치 구간"

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
    """뼈대 구간 하나의 기본 event."""

    base_id: str
    segment: SkeletonSegment
    event_type: EventType
    title: str
    description: str
    #: 이 기본 event 의 주인인 일정. 있으면 시간은 일정 시간이다.
    calendar: CalendarClue | None = None
    #: 일정이 들어오기 전 체류의 틀. 일정 event 가 체류 일부만 덮었을 때 나머지를 이 틀로 되살린다.
    stay: "BaseEvent | None" = None

    @property
    def start(self) -> datetime:
        return self.calendar.start if self.calendar and self.calendar.start else self.segment.start

    @property
    def end(self) -> datetime:
        return self.calendar.end if self.calendar and self.calendar.end else self.segment.end

    def as_prompt_dict(self) -> dict[str, object]:
        # 제목은 싣지 않는다. Timeline 이 근거의 주소로 쓴다.
        payload: dict[str, object] = {
            "id": self.base_id,
            "eventType": self.event_type.value,
            "description": self.description,
        }
        if self.calendar is not None:
            payload["calendar"] = self.calendar.clue_id
            payload["startAt"] = self.start.isoformat()
            payload["endAt"] = self.end.isoformat()
        return payload


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
    참석했는지는 모르므로 `일정이 있었음`·`일정이 있었다` 로 둔다.
    """

    return BaseEvent(
        base.base_id,
        base.segment,
        EventType.CALENDAR_EVENT,
        f"{calendar.title} 일정이 있었음",
        f"{{place}}에서 {calendar.title} 일정이 있었다.",
        calendar,
        base,
    )


def base_event_draft(
    base: BaseEvent,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    places: list[str] | None = None,
    client_event_id: str | None = None,
) -> TimelineEventDraft:
    """기본 event 를 틀 문장 그대로 draft event 로 만든다. 시간은 구간(일정 기본 event 는 일정)이나 그 일부다."""

    start = start or base.start
    end = end or base.end
    refs = [
        SourceRef(
            source_type=EventSourceType.STAY if record.is_stay else EventSourceType.MOVEMENT,
            raw_id=record.raw_id,
            reason=SEGMENT_REASON,
        )
        for record in base.segment.records
        if record.start <= end and start <= record.end
    ] or [
        SourceRef(
            source_type=EventSourceType.STAY if record.is_stay else EventSourceType.MOVEMENT,
            raw_id=record.raw_id,
            reason=SEGMENT_REASON,
        )
        for record in base.segment.records
    ]
    if base.calendar is not None:
        refs.append(
            SourceRef(source_type=EventSourceType.CALENDAR, raw_id=base.calendar.raw_id, reason="일정")
        )
    return TimelineEventDraft(
        client_event_id=client_event_id or f"base-{base.base_id}",
        event_type=base.event_type,
        title=base.title,
        description=base.description,
        start_time=start,
        end_time=end,
        confidence=0.5,
        inference_level=InferenceLevel.DIRECT,
        source_refs=refs,
        place_candidates=[] if base.event_type is EventType.MOVEMENT else list((places or [])[:4]),
        merge_reason="Timeline 이 쓰지 않은 기본 event 를 코드가 틀 문장으로 남겼다.",
        base_event_ids=[base.base_id],
    )
