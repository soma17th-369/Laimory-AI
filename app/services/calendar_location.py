"""캘린더-위치 상관 보강 (결정론적 confidence 규칙).

캘린더 일정과 같은 시간대의 체류(STAY)는 서로 다른 두 사건이 아니라 같은 사건의
두 얼굴이다. 일정은 "무엇을 하려 했는가"를, 체류는 "실제로 어디에 있었는가"를
말한다. 병합 자체는 Timeline Agent 프롬프트가 담당하고, 이 모듈은 병합된 draft 에서
**캘린더 `locationText` 와 STAY 의 `place`/`places`/`address` 가 같은 장소를 가리키는지**
를 결정론적으로 확인해 `confidence` 를 올린다.

두 근거가 서로를 확증하면(예: `locationText: 집(경기도 오산시 운암로 90)` ↔
`stay.address: 경기도 오산시 운암로 90`) 그 event 는 한쪽 source 의 추정이 아니라
교차 검증된 사실에 가깝다. LLM 이 이 보정을 매번 같은 크기로 해 주지는 않으므로
코드로 고정한다.
"""

from app.core.logging import get_logger
from app.schemas import (
    CalendarItem,
    EventSourceType,
    StayItem,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
)
from app.services.place_text import places_match
from app.services.source_lookup import source_identifier

logger = get_logger(__name__)

# 두 근거가 서로를 확증했을 때 올려 줄 confidence 폭과 상한.
# 교차 검증이 됐다고 해서 1.0 으로 단정하지는 않는다.
_CONFIDENCE_BOOST = 0.1
_MAX_CONFIDENCE = 0.95

__all__ = ["places_match", "reinforce_calendar_location"]


def _same_place(calendar: CalendarItem, stay: StayItem) -> bool:
    """캘린더의 장소 메모가 체류 장소와 같은 곳을 가리키는지 본다."""

    location_text = calendar.location_text
    if not location_text:
        return False
    return any(
        places_match(location_text, candidate)
        for candidate in (stay.place, stay.address, *stay.places)
    )


def _referenced(event: TimelineEventDraft, source_type: EventSourceType, lookup: dict) -> list:
    """event 가 근거로 삼은 입력 항목들을 sourceRefs 로 되짚는다."""

    return [
        lookup[ref.source_id]
        for ref in event.source_refs
        if ref.source_type is source_type and ref.source_id in lookup
    ]


def _matched_pair(
    event: TimelineEventDraft,
    calendars: dict[str, CalendarItem],
    stays: dict[str, StayItem],
) -> tuple[CalendarItem, StayItem] | None:
    """event 안에서 같은 장소를 가리키는 (캘린더, 체류) 한 쌍을 찾는다."""

    event_calendars = _referenced(event, EventSourceType.CALENDAR, calendars)
    if not event_calendars:
        return None
    event_stays = _referenced(event, EventSourceType.STAY, stays)

    for calendar in event_calendars:
        for stay in event_stays:
            if _same_place(calendar, stay):
                return calendar, stay
    return None


def reinforce_calendar_location(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> None:
    """캘린더와 체류가 같은 장소를 가리키는 event 의 confidence 를 올린다(in-place).

    캘린더 근거와 체류 근거를 **모두** 가진 event 만 대상이다. 둘 중 하나만 있으면
    확증할 것이 없으므로 손대지 않는다.
    """

    calendars = {
        identifier: item
        for item in request.calendars
        if (identifier := source_identifier(item))
    }
    stays = {
        identifier: item
        for item in request.stays
        if (identifier := source_identifier(item))
    }
    if not calendars or not stays:
        return

    for event in draft.events:
        matched = _matched_pair(event, calendars, stays)
        if matched is None:
            continue

        calendar, stay = matched
        before = event.confidence
        event.confidence = min(_MAX_CONFIDENCE, round(before + _CONFIDENCE_BOOST, 4))
        if event.confidence == before:
            continue

        logger.info(
            "캘린더-위치 상관으로 confidence 보강: event=%s, %.2f -> %.2f, "
            "locationText=%s, stay=%s",
            event.client_event_id,
            before,
            event.confidence,
            calendar.location_text,
            stay.place or stay.address,
        )
