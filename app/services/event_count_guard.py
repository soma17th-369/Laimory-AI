"""최종 event 개수 상한 (#118, #163).

프롬프트는 하루의 event 를 최대 10개로 구성하라고 지시한다. 지켰는지 재는 코드가 없으면
잘게 쪼개진 하루가 그대로 저장돼도 결과를 볼 때까지 모른다. 이 guard 는 그 초과를
드러내는 결정론적 안전망이다.

**넘으면 근거가 알림뿐인 event 부터 코드가 지운다(v3 세트, #163).** 알림 수가 적은 것부터,
같으면 늦게 시작한 것부터다. 예전에는 재기만 하고 자르지 않았다 — 코드가 고르면 캘린더·
사진처럼 반드시 남아야 할 근거를 잃기 때문이다. 알림만 근거인 event 는 그 근거가 없다.
운영에서 반응·짧은 답장·안내 알림이 event 가 되어 하루가 15개로 저장됐다.

알림만 근거인 event 를 다 지워도 넘으면 지금처럼 warning 만 남긴다. 그다음 무엇을 합칠지는
의미 판단이라 Repair 의 몫이다(`duration_guard` 와 같은 원칙).

상한은 세트에 따라 다르다. 10개는 Timeline·Repair v3 프롬프트가 지시하는 값이다.
v1·v2 Timeline 프롬프트에는 개수 지시가 없어, 그 세트에 10개를 재면 지시받은 적 없는
규칙으로 warning 이 붙고 v2 Repair 가 그것을 줄이려 든다. 그 세트는 예전 값 24개로 재고
지우지 않는다.

**v4 의 대화 event 는 10개와 상관없이 들어간다(#183, 사용자 결정 2026-11-02).** 세지도 지우지도
않고 하루 3개 제한만 받는다(`conversation_ids`). 기본 틀이 9~10개인 날 대화가 들어갈 자리가 없어
멘토의 조언처럼 구체적인 대화가 빠졌다. v3 는 그대로 대화까지 함께 센다.
"""

from app.core.logging import get_logger, log_fields
from app.schemas import (
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.conversation_guard import (
    is_conversation_event,
    notification_count,
    notification_only,
)

logger = get_logger(__name__)

#: 하루 타임라인의 최대 event 수. v3 프롬프트가 지시하는 값이다.
MAX_EVENT_COUNT = 10

#: 개수 지시가 없는 예전 세트(v1·v2)에 재는 값. 줄이기 전의 상한이다.
LEGACY_MAX_EVENT_COUNT = 24

_WARNING_ID_PREFIX = "warning-event-count-"

#: 지운 것을 알리는 warning. 검사 warning(`_WARNING_ID_PREFIX`)과 접두사가 겹치면 검사가
#: 다시 잴 때 함께 지워진다.
_TRIM_WARNING_ID_PREFIX = "warning-event-trim-"

_SAMPLE_LIMIT = 3


def _trim_order(event: TimelineEventDraft):
    """먼저 지울 event 가 앞이다. 알림이 적은 것, 같으면 늦게 시작한 것이다."""

    return (notification_count(event), -event.start_time.timestamp(), event.title)


def _counted(
    draft: TimelineDraft,
    conversation_ids: set[str] | None,
    aside: frozenset[str] | set[str],
) -> list[TimelineEventDraft]:
    """상한에 세는 event. `conversation_ids` 를 주면(v4) 대화 event 는 세지 않는다."""

    if conversation_ids is None:
        return list(draft.events)
    return [
        event
        for event in draft.events
        if not is_conversation_event(event, conversation_ids, aside)
    ]


def enforce_event_count(
    draft: TimelineDraft,
    *,
    limit: int = MAX_EVENT_COUNT,
    aside: frozenset[str] | set[str] = frozenset(),
    conversation_ids: set[str] | None = None,
) -> list[TimelineEventDraft]:
    """상한을 넘으면 근거가 알림뿐인 event 부터 지운다(in-place). 지운 event 를 돌려준다.

    `aside` 는 알림뿐인지 볼 때 세지 않는 근거다(`notification_only`). `conversation_ids` 를 주면
    대화 event 는 세지도 지우지도 않는다. 두 번 적용해도 결과가 같다.
    """

    counted = _counted(draft, conversation_ids, aside)
    before = len(counted)
    excess = before - limit
    if excess <= 0:
        return []

    removed = sorted(
        (event for event in counted if notification_only(event, aside)), key=_trim_order
    )[:excess]
    if not removed:
        return []

    removed_ids = {id(event) for event in removed}  # clientEventId 는 아직 중복일 수 있다
    draft.events = [event for event in draft.events if id(event) not in removed_ids]

    titles = ", ".join(event.title for event in removed[:_SAMPLE_LIMIT])
    if len(removed) > _SAMPLE_LIMIT:
        titles += f" 외 {len(removed) - _SAMPLE_LIMIT}건"
    draft.warnings.append(
        TimelineWarning(
            warning_id=f"{_TRIM_WARNING_ID_PREFIX}{len(draft.warnings) + 1:03d}",
            severity=TimelineWarningSeverity.LOW,
            message=(
                f"event 가 {before}개로 하루 최대 {limit}개를 넘어 근거가 알림뿐인 event "
                f"{len(removed)}건을 제외했습니다: {titles}"
            ),
        )
    )
    # 제목은 운영 이벤트로 나가지 않는다. 건수만 남긴다.
    logger.debug(
        "event 개수 정리",
        extra=log_fields(eventCountBefore=before, eventTrimmedCount=len(removed)),
    )
    return removed


def verify_event_count(
    draft: TimelineDraft,
    *,
    limit: int = MAX_EVENT_COUNT,
    aside: frozenset[str] | set[str] = frozenset(),
    conversation_ids: set[str] | None = None,
) -> None:
    """상한을 넘는 event 수를 경고하고 이전 검사 결과를 재계산한다."""

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    count = len(_counted(draft, conversation_ids, aside))
    if count <= limit:
        return

    message = (
        f"event 가 {count}개로 하루 최대 {limit}개를 넘었습니다. "
        "같은 장소의 이어진 체류, 같은 대화방의 대화, 같은 장면의 사진, "
        "한 여정의 짧은 이동부터 합쳐야 합니다."
    )
    if conversation_ids is not None:
        message = (
            f"대화를 뺀 event 가 {count}개로 하루 최대 {limit}개를 넘었습니다. "
            "같은 장소의 이어진 체류, 같은 장면의 사진, 한 여정의 짧은 이동부터 합쳐야 합니다."
        )
    draft.warnings.append(
        TimelineWarning(
            warning_id=f"{_WARNING_ID_PREFIX}001",
            severity=TimelineWarningSeverity.MEDIUM,
            message=message,
        )
    )
