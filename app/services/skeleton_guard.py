"""기본 event 구조 지키기 (#167·#169·#202, v4 세트).

하루의 event 는 코드가 정한다(`clue_bundles`, #202). Timeline·Repair 는 기본 event 마다 종류와 글만
쓰고, 확정 pass 는 확정할 때마다 **코드의 구조로 맞춘다**(`follow_base_events`). event 집합, 시간, 위치
기록·사진·일정 근거가 기본 event 의 것이 된다. 모델이 고르는 근거는 알림·대화뿐이다.

예전에는 Timeline 이 기본 event 위에서 event 를 꺼내고 합치고 경계를 옮겼고, 여기서 규칙마다 어긴 변경을
하나씩 되돌렸다(근거 없는 이동, 이동 구간 분리, 이동 구간 하나로 맞추기, 근거 시간 밖 event, 위치 구간
복원 …). 그 단계들은 모델이 고른 eventType(식사·수면)에 따라 시간을 다르게 고쳐, 같은 입력에서도 실행마다
event 수와 경계가 달랐다. 정답 구조가 코드에 있으니 한 번에 맞추는 것으로 대신했다.

남은 단계는 구조가 아닌 것을 지킨다. 대화 event 의 근거, 알림 근거의 시간, 틀이 없는 날의 알림 event,
제목의 `체류`, 그리고 v4 에 없는 `UNKNOWN` 이다.
"""

import re

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
from app.schemas.clues import NotificationClue, NotificationClueKind
from app.services.base_events import (
    NEEDS_WRITING,
    BaseEvent,
    base_event_draft,
    stay_title,
    structural_refs,
)
from app.services.clue_bundles import TimelineClues, place_only_raw_ids
from app.services.conversation_guard import (
    MAX_CONVERSATION_EVENTS,
    is_conversation_event,
    notification_only,
)
from app.services.location_metrics import SHORT_STAY_MAX
from app.services.narrative_place_guard import has_stay_word
from app.services.source_lookup import raw_id_of

__all__ = [
    "NEEDS_WRITING",
    "ensure_notification_events",
    "follow_base_events",
    "keep_conversation_evidence",
    "keep_stay_word_out",
    "restore_place_only_text",
    "retire_unknown_events",
    "settle_event_evidence",
]

logger = get_logger(__name__)

_WARNING_ID = "warning-skeleton-restored"

_SAMPLE_LIMIT = 3

#: 틀이 하나도 없는 날 그 자체로 event 가 되는 알림. 사용자가 했거나 끝낸 일을 말한다.
_NOTIFICATION_EVENT_KINDS = frozenset(
    {NotificationClueKind.PAYMENT, NotificationClueKind.RESERVATION, NotificationClueKind.ACTION_RESULT}
)
_RESTORED_CONFIDENCE = 0.6

#: 틀이 없는 날 알림뿐인 event 의 종류. 결제가 쇼핑인지 식사인지 코드는 모르므로 Repair 가 글을 쓰며 바꾼다.
_ALERT_EVENT_TYPES = {
    NotificationClueKind.CONVERSATION: EventType.SOCIAL,
    NotificationClueKind.PAYMENT: EventType.SHOPPING,
}
_TITLE_MAX = 30

#: 근거가 위치뿐이면 어디에 있었는지까지만 말하는 종류. `WORK`·`CLASS`·`REST` 는 User Memory 가
#: 그곳을 설명할 수 있어 넣지 않는다. 쇼핑·관람·용무는 User Memory 로 정하지 않는다(#172).
_PLACE_ONLY_TYPES = frozenset(
    {
        EventType.STAY,
        EventType.UNKNOWN,
        EventType.SLEEP,
        EventType.SOCIAL,
        EventType.PHOTO_MOMENT,
        EventType.SHOPPING,
        EventType.CULTURE,
        EventType.ERRAND,
    }
)

#: 활동 근거가 없는 체류의 문장. `{place}에서 ○○를 먹으며 시간을 보냈다` 는 활동을 말하므로 맞지 않는다.
_PLACE_ONLY_SENTENCE = re.compile(r"(?:.*에서 )?(?:시간|밤)을 보냈다\.?")


def follow_base_events(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """draft 를 코드가 정한 하루의 event 로 맞춘다(in-place, #202).

    "코드가 방문당 한 이벤트를 만들고 그 시간과 근거를 채우며 모델은 방문별 서술을 제공한다."

    - 기본 event 마다 event 하나다. 같은 기본 event 의 event 가 여럿이면 처음 것만 남는다.
    - 시간과 위치 기록·사진·일정 근거는 기본 event 의 값이다. 모델이 고른 알림·대화 근거는 둔다.
    - 이동·산책의 종류는 구간이 정한다.
    - 빠진 기본 event 는 틀 글로 되살린다. 일정·사진이 있으면 글은 Repair 가 쓴다(`NEEDS_WRITING`).
    - 기본 event 가 아닌 event(코드가 대화·알림으로 만든 event)는 위치 기록·사진·일정 근거를 갖지 않는다.
      **틀이 있는 날에는 대화 event 만 남는다**(#173, 사용자 결정 2026-10-31 — "알림은 기존 만들어진 틀의
      부가적인 근거가 되는 것"). 틀이 없는 날의 알림 event 는 `ensure_notification_events` 가 만든 그날의
      기록이라 둔다.

    Timeline 의 파싱이 이미 이 모양으로 만들므로 보통은 아무것도 바꾸지 않는다. Timeline 이 실패해 빈
    draft 가 오면 여기서 하루가 틀 글로 채워진다.
    """

    bundles = {
        bundle.base_event.base_id: bundle for bundle in clues.bundles if bundle.base_event is not None
    }
    structural = clues.structural_raw_ids
    conversation_ids = clues.conversation_ids
    seen: set[str] = set()
    kept: list[TimelineEventDraft] = []
    for event in draft.events:
        base_id = next((b for b in event.base_event_ids if b in bundles), None)
        chosen = [ref for ref in event.source_refs if ref.raw_id not in structural]
        if base_id is None:
            if len(chosen) < len(event.source_refs):
                event.source_refs = chosen
                event.merge_reason = NEEDS_WRITING
            alert_only = not any(ref.raw_id in conversation_ids for ref in event.source_refs)
            if not event.source_refs or (bundles and alert_only):
                continue
            # 틀이 있는 날 기본 event 가 아닌 event 는 대화 event 이고 종류는 `SOCIAL` 이다
            # (`add_conversation_event` 와 같다, #183). 대화를 식사로 쓰면 대화 개수 제한을 빠져나갔다.
            if bundles:
                event.event_type = EventType.SOCIAL
            event.base_event_ids = []
            kept.append(event)
            continue
        if base_id in seen:
            continue
        seen.add(base_id)
        base = bundles[base_id].base_event
        fixed = structural_refs(base)
        held = {ref.raw_id for ref in fixed}
        event.source_refs = [*fixed, *(ref for ref in chosen if ref.raw_id not in held)]
        event.start_time, event.end_time = base.start, base.end
        event.base_event_ids = [base_id]
        if base.fixed_type is not None:
            event.event_type = base.fixed_type
        kept.append(event)

    restored: list[str] = []
    for base_id, bundle in bundles.items():
        if base_id in seen:
            continue
        event = base_event_draft(
            bundle.base_event, places=bundle.places, client_event_id=f"base-{base_id}"
        )
        kept.append(event)
        restored.append(event.title)
    draft.events = kept

    draft.warnings = [w for w in draft.warnings if not w.warning_id.startswith(_WARNING_ID)]
    if restored:
        titles = ", ".join(restored[:_SAMPLE_LIMIT])
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_ID}-001",
                severity=TimelineWarningSeverity.MEDIUM,
                message=f"글이 없는 기본 event {len(restored)}건을 틀 글로 남겼습니다: {titles}",
            )
        )
        logger.debug("기본 event 되살림: %d건", len(restored))


def keep_conversation_evidence(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """대화 event 의 근거는 대화 알림뿐이다(in-place, #183, 사용자 결정 2026-11-02).

    "대화 이벤트는 오로지 대화알림을 근거로만 만들어져야 한다". 대화 알림만 남기고 시간을 그 대화가 온
    시간으로 맞춘다. 근거를 뺐으면 글은 Repair 가 쓴다. 기본 event 는 대화 event 가 아니다 — 대화가 그
    event 의 근거로 붙어도(`SUPPORT`) 그 event 의 시간과 근거는 코드가 정한 값이다(#202).
    """

    conversation_ids = clues.conversation_ids
    for event in draft.events:
        if not is_conversation_event(event, conversation_ids, clues.short_record_ids):
            continue
        kept = [ref for ref in event.source_refs if ref.raw_id in conversation_ids]
        if len(kept) < len(event.source_refs):
            event.source_refs = kept
            event.merge_reason = NEEDS_WRITING
        event.base_event_ids = []
        held = {ref.raw_id for ref in kept}
        talks = [
            clue
            for clue in clues.notifications
            if clue.first_at is not None and held & set(clue.raw_ids)
        ]
        if talks:
            event.start_time = min(clue.first_at for clue in talks)
            event.end_time = max(clue.last_at or clue.first_at for clue in talks)


def ensure_notification_events(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """틀이 하나도 없는 날 빠진 결제·예약·완료 알림을 event 로 되살린다(in-place).

    "사진·캘린더가 중심이고 없으면 알림이라도"(사용자 결정 2026-10-31). 위치도 사진도 일정도 없는 날은 알림이
    그 자체로 그날의 기록이다. 사진이나 일정이 있는 날은 되살리지 않는다 — 알림은 틀의 근거이지 event 가
    아니다(#173). 대화와 소식은 Timeline 이 고른다 — 답글 알림까지 대화로 분류돼 코드가 고를 수 없다.
    """

    if not all(notification_only(event, clues.short_record_ids) for event in draft.events):
        return
    _restore_notifications(
        draft, [clue for clue in clues.notifications if clue.kind in _NOTIFICATION_EVENT_KINDS]
    )
    # 사진도 일정도 결제도 없는 날은 남은 대화·소식이라도 그날의 기록이다(사용자 결정 2026-10-31).
    # event 0건이면 결과를 저장하지 못하고 1211 로 끝나 사용자에게 아무것도 보이지 않는다(#199).
    if not draft.events:
        talks = sorted(
            (clue for clue in clues.notifications if clue.kind is NotificationClueKind.CONVERSATION),
            key=lambda clue: -len(clue.raw_ids),
        )
        news = [clue for clue in clues.notifications if clue.kind is NotificationClueKind.NEWS]
        _restore_notifications(draft, [*talks[:MAX_CONVERSATION_EVENTS], *news])


def _restore_notifications(draft: TimelineDraft, picked: list[NotificationClue]) -> None:
    held = {ref.raw_id for event in draft.events for ref in event.source_refs}
    sleeps = [event for event in draft.events if event.event_type is EventType.SLEEP]
    for clue in picked:
        at = clue.first_at
        if at is None or held & set(clue.raw_ids):
            continue
        # 날짜를 모르는 예약은 오늘 일이 아니다.
        if clue.schedule_today is False or (
            clue.kind is NotificationClueKind.RESERVATION and clue.schedule_today is None
        ):
            continue
        if any(sleep.start_time <= at <= sleep.end_time for sleep in sleeps):
            continue
        draft.events.append(
            TimelineEventDraft(
                client_event_id=f"notification-restored-{clue.clue_id}",
                event_type=_ALERT_EVENT_TYPES.get(clue.kind, EventType.ERRAND),
                title=(clue.title or clue.counterpart or clue.kind.value)[:_TITLE_MAX],
                description=clue.summary or "",
                start_time=at,
                end_time=at,
                confidence=_RESTORED_CONFIDENCE,
                inference_level=InferenceLevel.DIRECT,
                source_refs=[
                    SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=raw_id)
                    for raw_id in clue.raw_ids
                ],
                merge_reason=NEEDS_WRITING,
            )
        )


def settle_event_evidence(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """모델이 고른 알림 근거를 event 와 맞춘다(in-place, 사용자 결정 2026-10-31).

    "이벤트와 그 근거 id 연계도 정합성이 무조건 맞아야해", "단서가 중복되서 나오는 거 아닌지 판단해".
    위치 기록·사진·일정은 코드가 정한 근거라 여기서 보지 않는다(#202).

    - 대화가 아닌 알림은 받은 시각이 event 시간 앞뒤 20분(`SHORT_STAY_MAX`) 안이어야 그 event 의
      근거다(#173). 15:28 결제가 18:17 식사의 근거로 붙었다. 근거가 모두 빠지는 event 는 건드리지 않는다.
    - 알림은 event 하나에만 둔다. 가장 짧은 event 가 갖는다 — Repair 가 대화 알림을 체류에도 적어 같은
      알림이 두 event 에 있었고 체류 문장이 대화로 덮였다.

    글은 건드리지 않는다.
    """

    spans = {
        raw_id: (clue.first_at, clue.last_at or clue.first_at)
        for clue in clues.notifications
        if clue.kind is not NotificationClueKind.CONVERSATION and clue.first_at is not None
        for raw_id in clue.raw_ids
    }
    for event in draft.events:
        low, high = event.start_time - SHORT_STAY_MAX, event.end_time + SHORT_STAY_MAX
        kept = [
            ref
            for ref in event.source_refs
            if (span := spans.get(ref.raw_id)) is None or (span[0] <= high and low <= span[1])
        ]
        if kept:
            event.source_refs = kept
    _keep_notifications_on_one_event(draft, request)


def restore_place_only_text(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """근거가 위치뿐인 event 의 글을 틀 문장으로 되돌린다. 사본에서만 돈다(`text_suggestions`).

    Repair 가 7시간 체류를 안쪽 점심과 같은 문장으로 고쳐 썼고, 일정 없는 저녁 체류가 `잠` 이 됐고, 사진을
    식사에 넘겨준 event 가 체류만 남긴 채 `OO와 함께했다` 로 남았다. 위치는 어디에 있었는지만 말한다.
    """

    says_only_where = place_only_raw_ids(request, clues)
    for event in draft.events:
        if event.event_type not in _PLACE_ONLY_TYPES or any(
            ref.raw_id not in says_only_where for ref in event.source_refs
        ):
            continue
        if (
            event.event_type is not EventType.STAY
            or "보냄" not in event.title
            or not _PLACE_ONLY_SENTENCE.fullmatch(event.description)
        ):
            _back_to_frame(event, clues)


def keep_stay_word_out(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """제목에 `체류` 를 남기지 않는다(in-place, v4, 사용자 결정 2026-11-01).

    "오후 체류는 절대 안돼", "timeline 이 체류라고 쓰면 안되는데". 재실행의 `체류` 제목은 Repair 가 마지막 반복에
    쓴 것이라 찾아도 고칠 기회가 없었다. 장소 자리처럼 코드가 지키는 자리다. 체류 틀로 되돌리기만 하고, 활동을
    말하던 event 는 Repair 가 다시 쓰게 넘긴다.
    """

    for event in draft.events:
        if has_stay_word(event.title):
            event.title = stay_title(clues.area(event.address))
            if event.event_type is not EventType.STAY:
                event.merge_reason = NEEDS_WRITING


def _keep_notifications_on_one_event(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """여러 event 가 가진 알림은 가장 짧은 event 에만 남긴다. 그것이 유일한 근거인 event 는 둔다."""

    notification_ids = {raw_id_of(item) for item in request.notifications}
    holders: dict[str, list[TimelineEventDraft]] = {}
    for event in draft.events:
        for raw_id in {ref.raw_id for ref in event.source_refs} & notification_ids:
            holders.setdefault(raw_id, []).append(event)
    for raw_id, events in holders.items():
        owner = min(events, key=lambda event: event.end_time - event.start_time)
        for event in events:
            rest = [ref for ref in event.source_refs if ref.raw_id != raw_id]
            if event is not owner and rest:
                event.source_refs = rest


def retire_unknown_events(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """v4 에는 `UNKNOWN` event 가 없다(in-place, 사용자 결정 2026-10-31).

    #172 로 체류는 `STAY`, #173 으로 알림만으로 된 event 는 없어져 `UNKNOWN` 이 쓰일 자리가 없다. 프롬프트와
    출력 schema 에서 뺐지만 모델이 쓸 수 있어 코드가 지킨다. 근거로 종류를 정하고 글은 건드리지 않는다.
    일정이 있으면 `CALENDAR_EVENT`, 위치 근거가 있으면 `STAY`, 사진뿐이면 `PHOTO_MOMENT` 이고, 알림뿐이면(틀이
    없는 날) 단서 종류로 정한다. `EventType.UNKNOWN` 값은 v1~v3 가 쓰므로 남는다.
    """

    location_ids = {raw_id_of(item) for item in [*request.stays, *request.movements]}
    kinds = {raw_id: clue.kind for clue in clues.notifications for raw_id in clue.raw_ids}
    for event in draft.events:
        if event.event_type is not EventType.UNKNOWN:
            continue
        refs = {ref.raw_id for ref in event.source_refs}
        if any(ref.source_type is EventSourceType.CALENDAR for ref in event.source_refs):
            event.event_type = EventType.CALENDAR_EVENT
        elif refs & location_ids:
            event.event_type = EventType.STAY
        elif any(ref.source_type is EventSourceType.PHOTO for ref in event.source_refs):
            event.event_type = EventType.PHOTO_MOMENT
        else:
            alerts = {kinds.get(raw_id) for raw_id in refs}
            kind = next(
                (k for k in (NotificationClueKind.CONVERSATION, NotificationClueKind.PAYMENT) if k in alerts),
                None,
            )
            event.event_type = _ALERT_EVENT_TYPES.get(kind, EventType.ERRAND)


def _back_to_frame(event: TimelineEventDraft, clues: TimelineClues) -> None:
    """event 를 그 기본 event 의 체류 틀 문장으로 되돌린다."""

    base = next(
        (bundle.base_event for b in event.base_event_ids if (bundle := clues.base_bundle(b)) and bundle.base_event),
        None,
    )
    if base is None:
        return
    event.event_type, event.title, event.description = _frame_of(base, clues)


def _frame_of(base: BaseEvent, clues: TimelineClues) -> tuple[EventType, str, str]:
    """체류의 틀 문장. 일정이 덮은 체류도 일정을 빼면 그 체류다."""

    if base.segment is not None and base.calendar is not None:
        return EventType.STAY, stay_title(clues.area(base.segment.address)), "{place}에서 시간을 보냈다."
    return base.event_type, base.title, base.description
