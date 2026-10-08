"""일정·사진 event 에 같은 시간의 체류를 위치 근거로 붙인다 (#138).

체류(STAY)는 그 시간에 사용자가 실제로 있던 곳이다. 같은 시간의 일정·사진은 그곳에서
일어난 일이므로, 체류는 그 event 의 **장소 근거**여야 한다. 그런데 Timeline 은 일정·사진만
근거로 대고 체류는 따로 카드로 만들거나 아예 참조하지 않는다. 그러면 일정·사진 event 의
`place` 가 비고, 같은 일을 두 번 그린 카드인지도 가릴 수 없다.

이 모듈은 규칙만으로 정해지는 일을 한다 — 시간이 겹치는 체류를 찾아 `sourceRefs` 에
더한다. event 의 시간·문장은 바꾸지 않고 아무것도 지우지 않는다. 더한 근거로 `place`·
`address` 를 채우는 것은 뒤이은 `resolve_places` 다.

체류 event 를 따로 남길지(그 일정·사진과 같은 것을 그린 카드인지)는 의미 판단이라 여기서
정하지 않는다. `location_only_overlap` 이 후보를 짚고 Repair 가 정한다.

장소 판정은 느슨하다. 시간이 겹친다는 사실이 이미 강한 근거라, 일정·사진이 장소를
말하는데 체류와 **이름조차 겹치지 않을 때만** 다른 곳으로 본다. 일정은 계획이라 실제로
다른 곳에 있었을 수 있는데, 그것을 가릴 수 있는 것이 그 장소 정보뿐이다.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, tzinfo

from app.core.logging import get_logger
from app.schemas import (
    CalendarItem,
    EventSourceType,
    EventType,
    PhotoItem,
    SourceRef,
    StayItem,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
)
from app.services.place_resolver import calendar_place_label, is_vague_place_label
from app.services.place_text import places_match
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

Span = tuple[datetime, datetime]

#: 위치를 근거로 붙이지 않는 event 종류. 이동의 장소는 도착지라 그 시간에 거기 있었다는
#: 뜻이 아니고, 수면·기상은 수면 guard 가 맡는다.
EXCLUDED_EVENT_TYPES = frozenset({EventType.MOVEMENT, EventType.SLEEP, EventType.WAKE_UP})

#: 위치만 있는 event 의 근거로 허용하는 종류. 걸음 수는 하루를 덮는 집계라 무엇을 했는지
#: 말해 주지 않는다.
_LOCATION_ONLY_SOURCES = frozenset({EventSourceType.STAY, EventSourceType.ACTIVITY})

#: 사건을 말해 주는 근거. 알림은 넣지 않는다 — 대화는 어디서든 오므로 그 시간의 체류가
#: 대화한 장소라는 뜻이 아니다.
_EVIDENCE_SOURCES = frozenset({EventSourceType.CALENDAR, EventSourceType.PHOTO})

#: 사람이 부르는 생활 장소명. 입력 장소명과 글자로 대조할 수 없어 장소 정보로 치지 않는다.
_LIFE_PLACE_LABELS = frozenset({"집", "본가", "회사", "직장", "학교", "숙소"})

#: 이 모듈이 붙인 근거의 표지. 붙인 근거를 나중에 걷어낼 때 이것으로 가린다.
LINK_REASON = "같은 시간에 머문 장소의 위치 기록"


@dataclass(frozen=True)
class StaySpan:
    """시간을 아는 체류 입력 하나."""

    raw_id: str
    start: datetime
    end: datetime
    item: StayItem


@dataclass(frozen=True)
class Evidence:
    """확정 단계가 여러 번 되짚는 입력 표."""

    stays: list[StaySpan]
    calendars: dict[str, CalendarItem]
    photos: dict[str, PhotoItem]
    tz: tzinfo


def collect_evidence(request: TimelineDraftRequest) -> Evidence:
    tz = resolve_timezone(request.timezone)
    stays: list[StaySpan] = []
    for item in request.stays:
        raw_id = raw_id_of(item)
        start = parse_datetime(item.start_at, tz)
        end = parse_datetime(item.end_at, tz) if item.end_at else None
        if not raw_id or start is None or end is None or end <= start:
            continue
        stays.append(StaySpan(raw_id, start, end, item))
    stays.sort(key=lambda stay: (stay.start, stay.end, stay.raw_id))
    return Evidence(
        stays=stays,
        calendars={
            raw_id: item for item in request.calendars if (raw_id := raw_id_of(item))
        },
        photos={raw_id: item for item in request.photos if (raw_id := raw_id_of(item))},
        tz=tz,
    )


def is_location_only(event: TimelineEventDraft) -> bool:
    """근거가 체류(와 걸음 수)뿐인 event. "여기 있었다" 말고는 말하는 것이 없다."""

    return (
        event.event_type not in EXCLUDED_EVENT_TYPES
        and any(ref.source_type is EventSourceType.STAY for ref in event.source_refs)
        and all(ref.source_type in _LOCATION_ONLY_SOURCES for ref in event.source_refs)
    )


def has_event_evidence(event: TimelineEventDraft) -> bool:
    """일정이나 사진이 근거인 event."""

    return event.event_type not in EXCLUDED_EVENT_TYPES and any(
        ref.source_type in _EVIDENCE_SOURCES for ref in event.source_refs
    )


def _evidence_moments(event: TimelineEventDraft, evidence: Evidence) -> Iterator[Span]:
    """event 가 근거로 댄 일정의 시간과 사진의 촬영 시각. 사진은 시작과 끝이 같다."""

    for ref in event.source_refs:
        if (calendar := evidence.calendars.get(ref.raw_id)) is not None:
            start = parse_datetime(calendar.start_at, evidence.tz)
            end = parse_datetime(calendar.end_at, evidence.tz) if calendar.end_at else None
            if start is not None and end is not None and end > start:
                yield start, end
        elif (photo := evidence.photos.get(ref.raw_id)) is not None:
            taken = parse_datetime(photo.taken_at, evidence.tz)
            if taken is not None:
                yield taken, taken


def own_place_texts(event: TimelineEventDraft, evidence: Evidence) -> list[str]:
    """event 가 스스로 말하는 장소. 체류와 대조해 다른 곳인지 볼 때 쓴다."""

    texts: list[str] = []
    for ref in event.source_refs:
        if (calendar := evidence.calendars.get(ref.raw_id)) is not None:
            label = calendar_place_label(calendar.location_text)
            if label and label not in _LIFE_PLACE_LABELS:
                texts.append(calendar.location_text or label)
        elif (photo := evidence.photos.get(ref.raw_id)) is not None:
            texts.extend(photo.places)
            if photo.address:
                texts.append(photo.address)
    if event.place and not is_vague_place_label(event.place):
        if event.place.strip() not in _LIFE_PLACE_LABELS:
            texts.append(event.place)
    return [text for text in texts if text and text.strip()]


def _stay_place_texts(stay: StayItem) -> list[str]:
    return [text for text in [stay.place, *stay.places, stay.address] if text]


def place_compatible(own_texts: list[str], stay: StayItem) -> bool:
    """장소가 명백히 다르지 않은가. 말하는 장소가 없으면 다를 근거도 없다."""

    if not own_texts:
        return True
    stay_texts = _stay_place_texts(stay)
    if not stay_texts:
        return True
    return any(places_match(own, other) for own in own_texts for other in stay_texts)


def _overlaps(stay: StaySpan, moment: Span) -> bool:
    start, end = moment
    if start == end:
        return stay.start <= start <= stay.end  # 사진은 그 시각을 품는 체류
    return stay.start < end and start < stay.end


def stays_for_event(event: TimelineEventDraft, evidence: Evidence) -> list[StaySpan]:
    """일정·사진 event 의 위치 근거가 될 체류. 시간이 겹치고 장소가 어긋나지 않는다."""

    moments = list(_evidence_moments(event, evidence))
    if not moments:
        return []
    own = own_place_texts(event, evidence)
    return [
        stay
        for stay in evidence.stays
        if any(_overlaps(stay, moment) for moment in moments)
        and place_compatible(own, stay.item)
    ]


def _drop_stale_links(draft: TimelineDraft) -> int:
    """일정·사진을 잃은 event 에서 이 모듈이 붙인 체류를 걷어낸다. 지운 event 수를 돌려준다.

    Repair 가 `update_event` 로 일정·사진 근거를 옮기면, 앞 회차에 붙인 체류만 남은 event 가
    생긴다. 붙인 근거는 일정·사진이 있을 때만 뜻이 있으므로 함께 걷어내고, 그 결과 근거가
    하나도 남지 않은 event 는 지운다. 사진 단일 귀속이 사진을 뺀 경우는 그 단계가 같은
    기준으로 지운다(`photo_guard._is_own_evidence`).
    """

    for event in draft.events:
        if has_event_evidence(event):
            continue
        kept = [ref for ref in event.source_refs if ref.reason != LINK_REASON]
        if len(kept) != len(event.source_refs):
            event.source_refs = kept
    before = len(draft.events)
    draft.events = [event for event in draft.events if event.source_refs]
    return before - len(draft.events)


def link_location_evidence(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """일정·사진 event 에 시간이 겹치는 체류를 장소 근거로 더한다(in-place).

    `place`·`address` 는 채우지 않는다. 확정 단계가 이 뒤에 `resolve_places` 를 돌린다.
    """

    dropped = _drop_stale_links(draft)
    if dropped:
        logger.debug("일정·사진을 잃고 붙인 근거만 남은 event %d건을 지웠습니다.", dropped)

    evidence = collect_evidence(request)
    if not evidence.stays:
        return

    linked = 0
    for event in draft.events:
        if not has_event_evidence(event):
            continue
        cited = {ref.raw_id for ref in event.source_refs}
        for stay in stays_for_event(event, evidence):
            if stay.raw_id in cited:
                continue
            event.source_refs.append(
                SourceRef(
                    source_type=EventSourceType.STAY,
                    raw_id=stay.raw_id,
                    reason=LINK_REASON,
                )
            )
            cited.add(stay.raw_id)
            linked += 1

    if linked:
        logger.debug("일정·사진 event 에 체류 위치 근거 %d건을 붙였습니다.", linked)
