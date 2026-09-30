"""대화로 만든 event 개수 제한 (#119).

대화 알림으로 만든 event 는 하루 최대 3개다. Notification Agent 의 프롬프트가 candidate 를
3개로 줄이지만(#116), Timeline 이 단서를 event 로 올리거나 같은 상대와의 끊어진 대화를
나누면 최종 draft 에서는 다시 3개를 넘을 수 있다. 이 guard 는 최종 draft 를 3개로 맞춘다.

대화 event 는 다음을 모두 만족하는 event 다.

- 근거가 전부 알림이다. 위치·사진·캘린더 근거가 함께 있으면 대화가 아니라 실제 사건이다.
- eventType 이 `SOCIAL`·`WORK`·`MEETING` 이다. 메신저로 온 결제·예약 안내로 만든 event 는
  다른 타입이 되므로 세지 않는다.
- 근거 알림이 전부 **메신저 정책이 있는 앱**에서 왔다.

**사전에 없는 앱의 알림은 대화로 세지 않는다.** 그 앱이 무엇인지 코드는 모른다. Telegram
같은 메신저일 수도, YouTube 같은 앱일 수도 있다. Notification Agent 도 같은 이유로 그런
알림을 대화로 묶지 않는다(#116). 그런 event 는 세지도 지우지도 않는다.

**알림이 많은 3개를 남기고 나머지는 지운다.** 기준이 알림 수 하나라 코드가 고를 수 있다.
Notification Agent 가 candidate 를 고르는 기준("대화 내용이 많을수록 중요한 대화")과 같은
방향이다. 두 단계의 기준이 다르면 앞에서 남긴 대화를 뒤에서 지운다.

처음에는 세기만 하고 Repair 가 고르게 했다. 실제 LLM 에 알림 수 순서로 지우라고 했더니
4번 중 3번은 알림이 가장 많은 단톡방을 잡담이라며 먼저 지웠다. 기준을 알림 수 하나로
정한 뒤에는 LLM 이 판단할 것이 남지 않아 코드로 옮겼다.
"""

from app.core.logging import get_logger, log_fields
from app.schemas import (
    EventSourceType,
    EventType,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: 하루에 남길 수 있는 대화 event 수.
MAX_CONVERSATION_EVENTS = 3

#: 대화로 만든 event 가 갖는 타입. 결제·예약 안내로 만든 event 는 여기 들지 않는다.
_CONVERSATION_EVENT_TYPES = frozenset(
    {EventType.SOCIAL, EventType.WORK, EventType.MEETING}
)

_WARNING_ID_PREFIX = "warning-conversation-limit-"

_SAMPLE_LIMIT = 3


def _app_names(request: TimelineDraftRequest) -> dict[str, str]:
    return {
        identifier: item.app_name
        for item in request.notifications
        if (identifier := raw_id_of(item))
    }


def notification_count(event: TimelineEventDraft) -> int:
    """event 가 근거로 댄 알림 수. 같은 알림을 두 번 적었어도 한 번으로 센다."""

    return len({ref.raw_id for ref in event.source_refs})


def conversation_events(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> list[TimelineEventDraft]:
    """대화로 만든 event 를 draft 의 순서대로 돌려준다(draft 를 바꾸지 않는다)."""

    # 앱 분류의 정본은 Notification Agent 가 쓰는 사전이다. guard 가 분류를 따로 가지면
    # 같은 앱을 두 곳이 다르게 본다. 모듈을 읽는 시점에 Agent 패키지까지 끌어오지 않도록
    # 함수 안에서 가져온다.
    from app.agents.events.notification.app_dictionary import (
        match_policy_ids,
        provides_conversation,
    )

    if not request.notifications:
        return []

    app_names = _app_names(request)
    found: list[TimelineEventDraft] = []

    for event in draft.events:
        if event.event_type not in _CONVERSATION_EVENT_TYPES:
            continue
        if not event.source_refs or any(
            ref.source_type is not EventSourceType.NOTIFICATION
            for ref in event.source_refs
        ):
            continue

        policies = [
            match_policy_ids(app_names.get(ref.raw_id)) for ref in event.source_refs
        ]
        # 사전에 없는 앱이나 결제·예약 앱의 알림이 하나라도 섞이면 대화로 세지 않는다.
        if all(ids and provides_conversation(ids) for ids in policies):
            found.append(event)

    return found


def _keep_order(event: TimelineEventDraft):
    """알림이 많은 대화가 먼저다. 같으면 먼저 시작한 대화다."""

    return (-notification_count(event), event.start_time, event.title)


def enforce_conversation_limit(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> list[TimelineEventDraft]:
    """대화 event 가 3개를 넘으면 알림이 많은 3개만 남긴다(in-place). 지운 event 를 돌려준다.

    두 번 적용해도 결과가 같다.
    """

    conversations = conversation_events(draft, request)
    if len(conversations) <= MAX_CONVERSATION_EVENTS:
        return []

    ranked = sorted(conversations, key=_keep_order)
    removed = ranked[MAX_CONVERSATION_EVENTS:]
    removed_ids = {id(event) for event in removed}  # clientEventId 는 아직 중복일 수 있다
    draft.events = [event for event in draft.events if id(event) not in removed_ids]

    titles = ", ".join(event.title for event in removed[:_SAMPLE_LIMIT])
    if len(removed) > _SAMPLE_LIMIT:
        titles += f" 외 {len(removed) - _SAMPLE_LIMIT}건"
    draft.warnings.append(
        TimelineWarning(
            warning_id=f"{_WARNING_ID_PREFIX}{len(draft.warnings) + 1:03d}",
            severity=TimelineWarningSeverity.LOW,
            message=(
                f"대화로 만든 event {len(conversations)}건 중 알림이 많은 "
                f"{MAX_CONVERSATION_EVENTS}건만 남기고 {len(removed)}건을 제외했습니다: {titles}"
            ),
        )
    )
    # 제목과 rawId 는 운영 이벤트로 나가지 않는다. 건수만 남긴다.
    logger.debug(
        "대화 event 개수 제한",
        extra=log_fields(
            conversationEventCount=len(conversations),
            conversationRemovedCount=len(removed),
        ),
    )
    return removed
