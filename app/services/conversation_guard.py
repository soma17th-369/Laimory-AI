"""대화로 만든 event 개수 제한 (#119).

대화 알림으로 만든 event 는 하루 최대 3개다. Notification Agent 의 프롬프트가 candidate 를
3개로 줄이지만(#116), Timeline 이 단서를 event 로 올리거나 같은 상대와의 끊어진 대화를
나누면 최종 draft 에서는 다시 3개를 넘을 수 있다. 이 guard 는 최종 draft 를 3개로 맞춘다.

대화 event 는 다음을 모두 만족하는 event 다.

- 근거가 전부 알림이다. 위치·사진·캘린더 근거가 함께 있으면 대화가 아니라 실제 사건이다.
- eventType 이 `MEAL`·`MOVEMENT` 가 아니다. 메신저로 온 결제·예약 안내로 만든 event 는 그
  타입이 되므로 세지 않는다. 예전에는 `SOCIAL`·`WORK`·`MEETING` 만 셌는데, 반응·짧은 답장
  같은 대화가 `UNKNOWN` event 가 되어 제한을 빠져나갔다(#163).
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

import re
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger, log_fields
from app.schemas import (
    ConversationDecision,
    EventSourceType,
    EventType,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.schemas.clues import NotificationClue
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: 하루에 남길 수 있는 대화 event 수.
MAX_CONVERSATION_EVENTS = 3

#: 메신저로 온 결제·예약 안내로 만든 event 의 타입. 대화가 아니라 세지 않는다.
_TRANSACTION_EVENT_TYPES = frozenset({EventType.MEAL, EventType.MOVEMENT})

#: 일정·사진이 있으면 대화가 아니라 실제로 있었던 일이다. 대화는 그 일의 보조 근거다.
_ACTIVITY_SOURCE_TYPES = frozenset({EventSourceType.CALENDAR, EventSourceType.PHOTO})

#: `mergeReason` 에 적은 알림 단서 id(`N5`).
_NOTIFICATION_ID = re.compile(r"(?<![0-9A-Za-z])N\d+(?![0-9A-Za-z])")

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


def notification_only(
    event: TimelineEventDraft, aside: frozenset[str] | set[str] = frozenset()
) -> bool:
    """근거가 알림뿐인 event 인가.

    `aside` 는 세지 않는 근거의 rawId 다. v4 의 단독 위치 기록은 event 를 만들지 않고 보태기만 하는
    근거라 붙어 있어도 실제 사건이 되지 않는다. Timeline 이 대화의 묶음 id 를 적으면 딸려 온다(#173).
    """

    refs = [ref for ref in event.source_refs if ref.raw_id not in aside]
    return bool(refs) and all(ref.source_type is EventSourceType.NOTIFICATION for ref in refs)


def is_conversation_event(
    event: TimelineEventDraft,
    conversation_ids: set[str],
    aside: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """v4 의 대화 event 인가(#183). 대화 자체를 기록한 event 다.

    대화 알림을 가졌고 일정·사진이 없으며, 근거가 알림뿐이거나 `SOCIAL` 이다. 일정·사진 없이 체류와
    대화뿐인 `SOCIAL` 도 대화 event 다 — 받은 메시지와 그 시간의 체류만으로는 만났다고 할 수 없다.
    `STAY`·`WORK` 처럼 틀이 있는 event 에 붙은 대화는 그 event 의 보조 근거라 여기 들지 않는다.
    """

    if event.event_type in _TRANSACTION_EVENT_TYPES:
        return False
    if not any(ref.raw_id in conversation_ids for ref in event.source_refs):
        return False
    if any(ref.source_type in _ACTIVITY_SOURCE_TYPES for ref in event.source_refs):
        return False
    return event.event_type is EventType.SOCIAL or notification_only(event, aside)


def conversation_events(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    conversation_ids: set[str] | None = None,
    *,
    aside: frozenset[str] | set[str] = frozenset(),
) -> list[TimelineEventDraft]:
    """대화로 만든 event 를 draft 의 순서대로 돌려준다(draft 를 바꾸지 않는다).

    `conversation_ids` 를 주면(v4) 앱 사전 대신 그것으로 센다. v4 는 Notification 단서 Agent 가
    알림을 대화로 묶어 두었고, 앱 사전으로 세면 대화로 쓰인 문자 알림(`메시지`)이 빠진다 — 실제
    재실행에서 대화 event 가 4개 남았다.
    """

    if conversation_ids is not None:
        # 대화에 거래 알림(당근)이 함께 붙어도 대화 event 로 센다 — 대화만 세면 그런 event 가 빠져
        # 4개가 남았다. 붙은 알림은 `keep_conversation_evidence` 가 뺀다(#183).
        return [
            event
            for event in draft.events
            if is_conversation_event(event, conversation_ids, aside)
        ]

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
        if event.event_type in _TRANSACTION_EVENT_TYPES:
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


@dataclass(frozen=True)
class UnusedConversations:
    """남긴 대화가 쓰이지 않은 자리. `event` 가 있으면 그 event 가 쓴다고 적고 잇지 않은 대화다."""

    conversation_ids: list[str]
    event: TimelineEventDraft | None = None

    def detail(self) -> dict[str, Any]:
        return {"conversationIds": list(self.conversation_ids)}


def find_unused_conversations(
    draft: TimelineDraft, conversations: list[NotificationClue], *, room: bool = True
) -> list[UnusedConversations]:
    """Timeline·Repair 가 다시 볼 대화를 찾는다(v4, #183). 고치지 않는다.

    Timeline 이 남기기로 정하고 어느 event 에도 잇지 않은 대화, 그런 대화가 없을 때 하나도 쓰이지 않은
    남긴 대화 전부, `mergeReason` 이 `N5` 처럼 적었는데 그 event 의 근거에 없는 대화다. 무엇을 기록할지와
    어디에 이을지는 내용을 읽어야 하는 판단이라 코드가 고르지 않는다. 실제 Timeline 은 13개를 어디에도
    잇지 않고 warning 에 "보조 근거로만 두었다" 고만 적었다. 전부 제외가 맞는 날도 있어 Repair 가 보고
    그대로 둘 수 있다.

    `room` 이 거짓이면 대화 event 를 더 만들 수 없는 날이다(대화 3개). `RECORD` 인데 쓰이지 않은 대화는
    개수로 빠진 것이라 찾지 않는다 — 다시 보여도 도구가 거절할 뿐이다.
    """

    if not conversations:
        return []

    by_id = {clue.clue_id: clue for clue in conversations}
    held = {ref.raw_id for event in draft.events for ref in event.source_refs}
    unused = [clue.clue_id for clue in conversations if not held & set(clue.raw_ids)]
    kept = {
        review.clue_id
        for review in draft.conversation_reviews
        if review.decision is ConversationDecision.SUPPORT
        or (review.decision is ConversationDecision.RECORD and room)
    }
    found: list[UnusedConversations] = []
    decided = [identifier for identifier in unused if identifier in kept]
    if decided or (room and len(unused) == len(by_id)):
        found.append(UnusedConversations(decided or unused))
    for event in draft.events:
        own = {ref.raw_id for ref in event.source_refs}
        named = [
            identifier
            for identifier in dict.fromkeys(_NOTIFICATION_ID.findall(event.merge_reason or ""))
            if identifier in by_id and not own & set(by_id[identifier].raw_ids)
        ]
        if named:
            found.append(UnusedConversations(named, event))
    return found


def _keep_order(event: TimelineEventDraft):
    """알림이 많은 대화가 먼저다. 같으면 먼저 시작한 대화다."""

    return (-notification_count(event), event.start_time, event.title)


def enforce_conversation_limit(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    conversation_ids: set[str] | None = None,
    *,
    aside: frozenset[str] | set[str] = frozenset(),
) -> list[TimelineEventDraft]:
    """대화 event 가 3개를 넘으면 알림이 많은 3개만 남긴다(in-place). 지운 event 를 돌려준다.

    두 번 적용해도 결과가 같다.
    """

    conversations = conversation_events(draft, request, conversation_ids, aside=aside)
    if len(conversations) <= MAX_CONVERSATION_EVENTS:
        return []

    if conversation_ids is None:
        ranked = sorted(conversations, key=_keep_order)
    else:
        # v4 는 메시지 수가 같으면 내용이 많은 대화를 남긴다(#183). 한 건짜리 대화끼리는 먼저 시작한 것이
        # 남아 `6월 말이었네` 같은 한마디가 멘토의 긴 조언을 밀어냈다.
        chars = {raw_id_of(item): len(item.text or "") for item in request.notifications}
        ranked = sorted(
            conversations,
            key=lambda event: (
                -notification_count(event),
                -sum(chars.get(ref.raw_id, 0) for ref in event.source_refs),
                event.start_time,
                event.title,
            ),
        )
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
