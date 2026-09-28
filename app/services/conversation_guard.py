"""대화로 만든 event 개수 검사 (#119).

대화 알림으로 만든 event 는 하루 최대 3개다. Notification Agent 의 프롬프트가 candidate 를
3개로 줄이지만(#116), Timeline 이 단서를 event 로 올리거나 같은 상대와의 끊어진 대화를
나누면 최종 draft 에서는 다시 3개를 넘을 수 있다. 이 guard 는 최종 draft 를 센다.

대화 event 는 다음을 모두 만족하는 event 다.

- 근거가 전부 알림이다. 위치·사진·캘린더 근거가 함께 있으면 대화가 아니라 실제 사건이다.
- eventType 이 `SOCIAL`·`WORK`·`MEETING` 이다. 메신저로 온 결제·예약 안내로 만든 event 는
  다른 타입이 되므로 세지 않는다.
- 근거 알림이 전부 **메신저 정책이 있는 앱**에서 왔다.

**사전에 없는 앱의 알림은 대화로 세지 않는다.** 그 앱이 무엇인지 코드는 모른다. Telegram
같은 메신저일 수도, YouTube 같은 앱일 수도 있다. Notification Agent 도 같은 이유로 그런
알림을 대화로 묶지 않고 모델이 내용으로 판단하게 한다(#116). 여기서는 그런 event 를
"대화 여부 미정"으로 따로 넘기고, 대화인지는 Repair 가 내용을 읽고 정한다.

**세기만 하고 지우지 않는다.** 무엇이 중요한 대화인지는 내용을 봐야 안다. 알림 수로 골라
지우면 짧지만 중요한 연락이 긴 잡담에 밀려 빠진다. 참조한 알림 수는 참고 값으로만 넘긴다.
"""

from dataclasses import dataclass, field

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


@dataclass
class ConversationCount:
    """최종 draft 의 대화 event."""

    #: 근거 알림이 전부 메신저 정책이 있는 앱에서 온 event.
    conversations: list[TimelineEventDraft] = field(default_factory=list)
    #: 근거에 사전에 없는 앱의 알림이 섞인 event. 대화인지는 코드가 알 수 없다.
    undetermined: list[TimelineEventDraft] = field(default_factory=list)

    @property
    def over_limit(self) -> bool:
        """코드가 아는 대화 event 만으로 상한을 넘었는가."""

        return len(self.conversations) > MAX_CONVERSATION_EVENTS

    @property
    def needs_review(self) -> bool:
        """미정까지 합치면 상한을 넘는가. Repair 가 판단할 일이 있다는 뜻이다."""

        return (
            len(self.conversations) + len(self.undetermined) > MAX_CONVERSATION_EVENTS
        )

    def detail(self) -> dict[str, object]:
        def listed(events: list[TimelineEventDraft]) -> list[dict[str, object]]:
            return [
                {
                    "clientEventId": event.client_event_id,
                    "title": event.title,
                    "notificationCount": len(event.source_refs),
                }
                for event in events
            ]

        return {
            "limit": MAX_CONVERSATION_EVENTS,
            "conversationEvents": listed(self.conversations),
            "undeterminedEvents": listed(self.undetermined),
        }


def _app_names(request: TimelineDraftRequest) -> dict[str, str]:
    return {
        identifier: item.app_name
        for item in request.notifications
        if (identifier := raw_id_of(item))
    }


def count_conversation_events(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> ConversationCount:
    """대화 event 와 대화 여부 미정 event 를 가려 센다(draft 를 바꾸지 않는다)."""

    # 앱 분류의 정본은 Notification Agent 가 쓰는 사전이다. guard 가 분류를 따로 가지면
    # 같은 앱을 두 곳이 다르게 본다. 모듈을 읽는 시점에 Agent 패키지까지 끌어오지 않도록
    # 함수 안에서 가져온다.
    from app.agents.events.notification.app_dictionary import (
        match_policy_ids,
        provides_conversation,
    )

    count = ConversationCount()
    if not request.notifications:
        return count

    app_names = _app_names(request)

    for event in draft.events:
        if event.event_type not in _CONVERSATION_EVENT_TYPES:
            continue
        if not event.source_refs or any(
            ref.source_type is not EventSourceType.NOTIFICATION
            for ref in event.source_refs
        ):
            continue

        has_unlisted_app = False
        has_other_policy = False
        for ref in event.source_refs:
            policy_ids = match_policy_ids(app_names.get(ref.raw_id))
            if not policy_ids:
                has_unlisted_app = True
            elif not provides_conversation(policy_ids):
                has_other_policy = True

        if has_other_policy:
            continue  # 결제·예약 앱의 알림이 근거다. 대화가 아니다.
        if has_unlisted_app:
            count.undetermined.append(event)
        else:
            count.conversations.append(event)

    return count


def verify_conversation_limit(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> ConversationCount:
    """대화 event 가 상한을 넘으면 warning 을 남기고 이전 검사 결과를 재계산한다."""

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    count = count_conversation_events(draft, request)
    if not count.over_limit:
        return count

    draft.warnings.append(
        TimelineWarning(
            warning_id=f"{_WARNING_ID_PREFIX}001",
            severity=TimelineWarningSeverity.MEDIUM,
            message=(
                f"대화로 만든 event 가 {len(count.conversations)}개로 하루 최대 "
                f"{MAX_CONVERSATION_EVENTS}개를 넘었습니다. 중요한 대화 "
                f"{MAX_CONVERSATION_EVENTS}개만 남겨야 합니다."
            ),
        )
    )
    logger.debug(
        "대화 event 상한 초과",
        extra=log_fields(
            conversationEventCount=len(count.conversations),
            undeterminedEventCount=len(count.undetermined),
        ),
    )
    return count
