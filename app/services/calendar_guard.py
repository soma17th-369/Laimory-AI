"""캘린더 일정 누락 방지.

캘린더는 위치와 함께 하루의 뼈대다. 위치는 센서가 남긴 흔적이지만 **캘린더는 사용자가
직접 손으로 적어 둔 계획**이라, 빠뜨리면 사용자가 가장 먼저 알아챈다.

그런데 Timeline Agent 는 캘린더 일정을 통째로 버린다. 실제 출력에서 Calendar Agent 는
`09:00~23:00 ASM 프로젝트 MVP 개발` 후보를 정확히 만들었는데, 최종 draft 에는 그 일정을
가리키는 event 가 하나도 없었다. 프롬프트가 "하루를 덮는 긴 일정은 통째로 하나의 event 로
만들지 말고 배경 맥락으로 두라"고 안내한 것을, LLM 이 "event 를 만들지 말라"로 읽었다.

프롬프트는 고쳤지만 그것만 믿지 않는다. 이 모듈이 최종 draft 에서 **어느 event 도
참조하지 않은 캘린더 일정**을 찾아 event 로 되살린다.

지어내는 것이 아니다. 제목·시각·장소는 전부 사용자가 캘린더에 적어 둔 값 그대로다.
다만 "일정이 잡혀 있었다"와 "그 시간 내내 그 일을 했다"는 다르므로, confidence 를 낮게
두고 `uncertainty` 로 한계를 남긴다.
"""

from app.core.logging import get_logger
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.place_resolver import calendar_place_label
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

#: 되살린 일정의 confidence. "일정이 잡혀 있었다"는 확실하지만 "그 일을 했다"는 아니다.
_RESTORED_CONFIDENCE = 0.6

_RESTORED_NOTE = (
    "캘린더에 일정이 잡혀 있었다는 것까지가 근거다. 그 시간 내내 실제로 그 일을 했는지는 "
    "알 수 없다."
)

#: 한 warning 에 담을 예시 개수.
_MAX_EXAMPLES = 3


def referenced_calendar_ids(draft: TimelineDraft) -> set[str]:
    """draft 의 event 들이 근거로 삼은 캘린더 rawId 집합."""

    return {
        ref.raw_id
        for event in draft.events
        for ref in event.source_refs
        if ref.source_type is EventSourceType.CALENDAR
    }


def ensure_calendar_events(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """어느 event 도 참조하지 않은 캘린더 일정을 event 로 되살린다(in-place).

    되살린 event 는 임시 `clientEventId` 를 갖는다. repair 마지막의 `renumber_events` 가
    정렬 후 최종 id 를 다시 부여하므로 여기서는 유일하기만 하면 된다.

    호출 위치는 repair 앞쪽이다. 되살린 event 도 window 클램프·장소 확정·정렬을 똑같이
    거쳐야 하기 때문이다.
    """

    if not request.calendars:
        return

    tz = resolve_timezone(request.timezone)
    referenced = referenced_calendar_ids(draft)
    restored: list[str] = []

    for index, item in enumerate(request.calendars, start=1):
        identifier = raw_id_of(item)
        if not identifier or identifier in referenced:
            continue

        start = parse_datetime(item.start_at, tz)
        if start is None:
            # 일정 제목은 사용자 콘텐츠라 남기지 않는다. 어느 일정인지는 Langfuse 의
            # 입력 스냅샷으로 되짚는다.
            logger.debug("캘린더 시작 시각을 읽지 못해 되살리지 못했습니다.")
            continue
        end = parse_datetime(item.end_at, tz) if item.end_at else None
        if end is None or end < start:
            end = start

        draft.events.append(
            TimelineEventDraft(
                client_event_id=f"calendar-restored-{index:03d}",
                event_type=EventType.CALENDAR_EVENT,
                title=item.title,
                description="캘린더에 적어 둔 일정이다.",
                place_label=calendar_place_label(item.location_text),
                start_time=start,
                end_time=end,
                confidence=_RESTORED_CONFIDENCE,
                inference_level=InferenceLevel.DIRECT,
                source_refs=[
                    SourceRef(source_type=EventSourceType.CALENDAR, raw_id=identifier)
                ],
                uncertainty=[_RESTORED_NOTE],
            )
        )
        restored.append(item.title)

    if not restored:
        return

    shown = ", ".join(restored[:_MAX_EXAMPLES])
    if len(restored) > _MAX_EXAMPLES:
        shown += f" 외 {len(restored) - _MAX_EXAMPLES}건"

    draft.warnings.append(
        TimelineWarning(
            warning_id=f"warning-calendar-{len(draft.warnings) + 1:03d}",
            severity=TimelineWarningSeverity.MEDIUM,
            message=(
                f"timeline 에 반영되지 않은 캘린더 일정 {len(restored)}건을 되살렸습니다: {shown}"
            ),
        )
    )
    logger.debug("누락된 캘린더 일정 %d건을 event 로 되살렸습니다.", len(restored))
