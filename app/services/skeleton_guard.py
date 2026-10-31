"""기본 event 구조 지키기 (#167·#169, v4 세트).

Timeline v4 는 코드가 만든 기본 event(뼈대 구간마다 하나) 위에서 근거를 더하고, 근거가 있으면
구조를 바꾼다(나누기, 이어진 체류 합치기, 경계 조정). 코드는 사용자가 이미 정한 규칙만 검사하고
**어긴 변경 하나만 되돌린다.** 나머지 변경은 Timeline 의 판단이라 받아들인다.

1. 이동 기록도 교통 결제·예약도 없는 `MOVEMENT` 는 지운다 — 기록되지 않은 이동(빈 시간)은
   event 가 아니다.
2. 이동은 체류와 합치지 않는다(#134). 이동 구간 기록을 다른 event 에서 빼고 걸친 끝을 자른다.
3. event 는 자기 근거의 시간과 겹친다. 근거와 시간이 겹치지 않는 event 는 지운다 — 실제 Timeline
   은 낮의 체류를 근거로 22:00~06:00 밤 event 를 만들었다. 위치 근거가 없는 event 는 근거의
   시간 밖으로 늘이지 않는다.
4. 기본 event 의 시간은 어느 event 가 덮는다. 덮이지 않은 구간 시간(20분 초과)은 틀 문장 event 로
   되살린다 — 실제 Timeline 은 집 체류 묶음을 통째로 빼먹거나, 사무실 체류의 오후를 비웠다.

예전에는 "구간의 기록이 어느 event 근거에 들어 있는가" 로 구간이 쓰였는지 봤다. 그러면 근거로만
대고 시간을 일부만 쓴 event 가 있어도 쓰인 것으로 보여 구멍이 남았다. 지금은 시간으로 본다.
"""

import re
from collections.abc import Callable
from datetime import timedelta

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
from app.services.base_events import BaseEvent, base_event_draft
from app.services.calendar_guard import RESTORED_CALENDAR_DESCRIPTION
from app.services.clue_bundles import TimelineClues
from app.services.conversation_guard import MAX_CONVERSATION_EVENTS
from app.services.location_metrics import SHORT_STAY_MAX
from app.services.location_skeleton import SegmentKind
from app.services.meal_guard import MEAL_STAY_MAX_DURATION
from app.services.photo_guard import CREATED_PHOTO_TITLE
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

_WARNING_ID = "warning-skeleton-restored"

#: Timeline 이 쓰는 시각의 단위. 기록의 초 단위 경계와 이만큼은 어긋난다.
_MINUTE = timedelta(minutes=1)

#: 구간 근거가 event 시간과 이만큼(또는 event 길이의 절반) 겹치면 그 event 의 근거다.
_MIN_OVERLAP = timedelta(minutes=30)


#: 일정 기본 event 의 주인이 될 수 없는 종류. 사진·`보낸 시간` 은 일정이 말하는 일이 아니다.
_NOT_CALENDAR_OWNER = frozenset({EventType.STAY, EventType.UNKNOWN, EventType.PHOTO_MOMENT})

#: 위치 기록이 없는 날 그 자체로 event 가 되는 알림. 사용자가 했거나 끝낸 일을 말한다.
_NOTIFICATION_EVENT_KINDS = frozenset(
    {NotificationClueKind.PAYMENT, NotificationClueKind.RESERVATION, NotificationClueKind.ACTION_RESULT}
)
_RESTORED_CONFIDENCE = 0.6
_TITLE_MAX = 30

#: `merge_reason` 표지. 코드가 만들었거나 구조를 바꿔 Repair 가 글을 써야 하는 event 다.
NEEDS_WRITING = "코드가 구조를 정했고 제목·문장은 Repair 가 근거로 쓴다."

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


def _owns_calendar(event: TimelineEventDraft, base: BaseEvent) -> bool:
    """그 event 가 일정 event 로 남을 수 있는가.

    식사는 식사 약속처럼 일정이 식사일 때만이다(식당에 머문 식사 상한 3시간). 더 긴 일정을 식사로 두면
    식사 시간이 일정을 잘라 회의가 17:24~17:44 가 됐다.
    """

    if event.event_type is EventType.MEAL:
        return base.end - base.start <= MEAL_STAY_MAX_DURATION
    return event.event_type not in _NOT_CALENDAR_OWNER


def keep_calendar_base_events(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """일정이 주인인 기본 event 는 event 하나다(in-place, 사용자 결정 2026-10-30).

    "캘린더의 경우 Timeline이 처리하기 전에 Location 의 결과물에다가 박아넣어버려". 코드가 일정을
    기본 event 에 넣어도 Timeline 이 같은 체류로 `보낸 시간` 을 따로 쓰거나(4번 중 1번), 일정 event 에
    일정 근거를 빠뜨려 캘린더 복원이 일정을 하나 더 만들었다(4번 중 1번).

    그 기본 event 에서 온 event 중 체류 구간을 덮는 것(구간 중 event 밖 시간이 20분 이하)은 같은
    사건이다. 일정을 가진 event 가 주인이고, 없으면 활동을 쓴 event 에 일정을 붙이고, 그것도 없으면
    일정 틀 문장으로 바꾼다. 나머지는 주인의 근거로 합친다. 시간은 일정 시간이다. 체류 일부에서
    끝나는 event(그 안의 식사)는 그대로다.
    """

    merged: set[int] = set()
    for bundle in clues.bundles:
        base = bundle.base_event
        if base is None or base.calendar is None:
            continue
        segment = base.segment
        covering = [
            event
            for event in draft.events
            if id(event) not in merged
            and base.base_id in event.base_event_ids
            and max(timedelta(0), event.start_time - segment.start)
            + max(timedelta(0), segment.end - event.end_time)
            <= SHORT_STAY_MAX
        ]
        if not covering:
            continue
        calendar_id = base.calendar.raw_id
        owner = next(
            (e for e in covering if any(ref.raw_id == calendar_id for ref in e.source_refs)),
            None,
        ) or next((e for e in covering if _owns_calendar(e, base)), None)
        # 일정을 가진 event 라도 일정 event 로 남을 수 없으면 되돌린다. Repair 가 회의 event 를 식사로
        # 바꾸며 묶음 id 로 일정을 다시 담자, 식사 시간이 그 event 를 20분으로 줄여 회의 일정이
        # 17:24~17:44 가 됐고 일정이 참조되고 있어 캘린더 복원도 돌지 않았다.
        if owner is None or not _owns_calendar(owner, base):
            owner = owner or covering[0]
            owner.event_type = base.event_type
            owner.merge_reason = NEEDS_WRITING
        if not any(ref.raw_id == calendar_id for ref in owner.source_refs):
            owner.source_refs.append(
                SourceRef(source_type=EventSourceType.CALENDAR, raw_id=calendar_id, reason="일정")
            )
        owner.start_time, owner.end_time = base.start, base.end
        held = {ref.raw_id for ref in owner.source_refs}
        for other in covering:
            if other is owner:
                continue
            owner.source_refs.extend(ref for ref in other.source_refs if ref.raw_id not in held)
            held |= {ref.raw_id for ref in other.source_refs}
            merged.add(id(other))
        # 그 일정의 근거는 주인 하나에만 둔다. 재실행에서 일정 시간과 조금 겹친 이동·다른 장소 체류가
        # 같은 일정을 근거로 가졌다. 근거가 그것뿐인 event 는 건드리지 않는다.
        for event in draft.events:
            if event is owner or id(event) in merged:
                continue
            rest = [ref for ref in event.source_refs if ref.raw_id != calendar_id]
            if rest:
                event.source_refs = rest
    if merged:
        draft.events = [event for event in draft.events if id(event) not in merged]
        logger.debug("일정 기본 event 지키기: %d건 합침", len(merged))


def ensure_notification_events(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """위치 기록이 없는 날 빠진 결제·예약·완료 알림을 event 로 되살린다(in-place).

    "위치가 없을 때도 v3 에서 나오는 거랑 거의 똑같이 나와야". 그날은 알림이 붙을 구간이 없어 알림이 그
    자체로 그날의 일인데, Timeline 은 붙일 곳이 없다며 버렸다(v3 8회 22건, v4 12회 2건). 위치 기록이 있는
    날에는 돌지 않는다. 대화와 소식은 Timeline 이 고른다 — 답글 알림까지 대화로 분류돼 코드가 고를 수 없다.
    """

    if clues.skeleton.has_location:
        return
    _restore_notifications(
        draft, [clue for clue in clues.notifications if clue.kind in _NOTIFICATION_EVENT_KINDS]
    )
    # 사진도 일정도 결제도 없는 날은 남은 대화·소식이라도 그날의 기록이다(사용자 결정 2026-10-31).
    # event 0건은 정상 결과로 저장돼 사용자에게 아무것도 보이지 않는다.
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
        # 날짜를 모르는 예약은 오늘 일이 아니다. 수면과 겹친 event 는 확정이 지운다.
        if clue.schedule_today is False or (
            clue.kind is NotificationClueKind.RESERVATION and clue.schedule_today is None
        ):
            continue
        if any(sleep.start_time <= at <= sleep.end_time for sleep in sleeps):
            continue
        draft.events.append(
            TimelineEventDraft(
                client_event_id=f"notification-restored-{clue.clue_id}",
                event_type=EventType.UNKNOWN,
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


def hand_restored_events_to_repair(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """코드가 되살린 사진·일정 event 의 고정 문장을 치우고 Repair 에 맡긴다(in-place, 사용자 결정 2026-10-31).

    `사진을 남겼다` 로는 사진의 내용이 사라진다. Repair 가 쓰기 전까지 사진 단서의 말을 그대로 두고, 일정은
    제목만 두고 문장을 비운다. Repair 가 실패해도 고정 문장이 나가지 않는다.
    """

    photos = {clue.raw_id: clue for clue in clues.photos}
    for event in draft.events:
        if event.description == RESTORED_CALENDAR_DESCRIPTION:
            event.description = ""
            event.merge_reason = NEEDS_WRITING
        elif event.title == CREATED_PHOTO_TITLE:
            event.merge_reason = NEEDS_WRITING
            clue = next((photos[r.raw_id] for r in event.source_refs if r.raw_id in photos), None)
            told = (clue.activity or clue.description or "").strip(" .") if clue else ""
            event.description = f"{told}." if told else ""
            if told:
                event.title = told[:_TITLE_MAX]


def settle_event_evidence(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """시간이 모두 정해진 뒤 event 와 근거를 맞춘다(in-place, 사용자 결정 2026-10-31).

    "이벤트와 그 근거 id 연계도 정합성이 무조건 맞아야해", "단서가 중복되서 나오는 거 아닌지 판단해".

    - event 시간과 맞지 않는 체류·이동 근거를 뺀다. 기본 event 에서 온 event 는 그 구간의 기록을 모두
      근거로 받아, 17:24 식사의 `sourceRawIds` 에 12:08~14:38 체류 기록이 실렸다(12개 하루에서 근거 295개
      중 58개). 앞뒤 20분(`SHORT_STAY_MAX`)으로 재고, 근거가 모두 빠지는 event 는 건드리지 않는다 — 시간
      밖 event 는 `remove_events_outside_evidence` 의 몫이다.
    - 알림은 event 하나에만 둔다. 사진처럼 꺼낸 event 가 갖는다 — Repair 가 대화 알림을 체류에도 적어
      같은 알림이 두 event 에 있었고 체류 문장이 대화로 덮였다.
    - 근거가 위치뿐인 event 를 같은 기본 event 의 다른 event 가 시간째 덮으면 그 event 의 위치 근거로 합친다.
      사진 하나로 체류 전체를 덮은 event 옆에 같은 시간의 `보낸 시간` 이 나란히 남았다.

    글은 건드리지 않는다. 근거가 위치뿐인데 활동을 말하는 문장은 `restore_place_only_text` 가 제안으로 넘긴다.
    """

    spans = clues.skeleton.record_spans()
    spans.update({record.raw_id: (record.start, record.end) for record in clues.skeleton.isolated})
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
    location_ids = {raw_id_of(item) for item in [*request.stays, *request.movements]}
    absorbed: set[int] = set()
    for event in draft.events:
        if event.event_type not in _PLACE_ONLY_TYPES or any(
            ref.raw_id not in location_ids for ref in event.source_refs
        ):
            continue
        cover = _event_covering(event, draft, absorbed)
        if cover is not None:
            held = {ref.raw_id for ref in cover.source_refs}
            cover.source_refs = [
                *cover.source_refs,
                *(ref for ref in event.source_refs if ref.raw_id not in held),
            ]
            absorbed.add(id(event))
    draft.events = [event for event in draft.events if id(event) not in absorbed]


def restore_place_only_text(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """근거가 위치뿐인 event 의 글을 틀 문장으로 되돌린다. 사본에서만 돈다(`text_suggestions`).

    Repair 가 7시간 체류를 안쪽 점심과 같은 문장으로 고쳐 썼고, 일정 없는 저녁 체류가 `잠` 이 됐고, 사진을
    식사에 넘겨준 event 가 체류만 남긴 채 `OO와 함께했다` 로 남았다. 위치는 어디에 있었는지만 말한다.

    오늘 일이 아닌 알림(`scheduleToday` 가 거짓)도 그 체류에서 한 일을 말하지 않는다. 다음 주 예매 알림이 온
    영화관 체류를 Timeline 이 관람으로 썼고, 그 알림이 근거로 붙어 위치뿐이 아닌 것으로 보여 빠져나갔다(#172).
    """

    says_only_where = {raw_id_of(item) for item in [*request.stays, *request.movements]} | {
        raw_id
        for clue in clues.notifications
        if clue.schedule_today is False
        for raw_id in clue.raw_ids
    }
    for event in draft.events:
        if event.event_type not in _PLACE_ONLY_TYPES or any(
            ref.raw_id not in says_only_where for ref in event.source_refs
        ):
            continue
        if (
            event.event_type is not EventType.STAY
            or "보낸" not in event.title
            or not _PLACE_ONLY_SENTENCE.fullmatch(event.description)
        ):
            _back_to_frame(event, clues)


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


def _event_covering(
    event: TimelineEventDraft, draft: TimelineDraft, absorbed: set[int]
) -> TimelineEventDraft | None:
    """같은 기본 event 에서 와서 그 event 의 시간을 모두 덮는 event. `absorbed` 는 이미 합친 event 다.

    기본 event 가 같아야 한다. 긴 일정 안에 잠깐 다른 장소에 간 체류는 뼈대로 남는다(사용자 결정).
    """

    return next(
        (
            other
            for other in draft.events
            if other is not event
            and id(other) not in absorbed
            and other.event_type is not EventType.MOVEMENT
            and set(other.base_event_ids) & set(event.base_event_ids)
            and other.start_time <= event.start_time
            and event.end_time <= other.end_time
        ),
        None,
    )


def ensure_skeleton_segments(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """기본 event 의 시간 중 어느 event 도 덮지 않은 곳을 틀 문장 event 로 되살린다(in-place)."""

    restored: list[str] = []
    for bundle in clues.bundles:
        base = bundle.base_event
        if base is None:
            continue
        segment = base.segment
        # 일정 기본 event 는 그 일정을 가진 event 도 구간을 덮는다 — 일정이 빠져 캘린더 복원이 되살린
        # 일정 event 는 기본 event 도 체류 기록도 갖지 않는다.
        segment_ids = set(segment.raw_ids) | ({base.calendar.raw_id} if base.calendar else set())
        covering = [
            (max(event.start_time, segment.start), min(event.end_time, segment.end))
            for event in draft.events
            if base.base_id in event.base_event_ids
            or any(ref.raw_id in segment_ids for ref in event.source_refs)
        ]
        for start, end in _uncovered(segment.start, segment.end, covering):
            whole = start == segment.start and end == segment.end
            # 기록이 덮는 시간만 되살린다. 구간 안에서 기록이 없는 틈(같은 장소로 묶인 수집 공백)은
            # 비워 둔다 — 그 틈을 채운 조각은 근거 기록과 겹치지 않아 지명도 장소 후보도 없는
            # `보낸 시간` 이 됐다(실제 재실행).
            recorded = [r for r in segment.records if r.start < end and start < r.end]
            if not recorded:
                continue
            start = max(start, min(r.start for r in recorded))
            end = min(end, max(r.end for r in recorded))
            if not whole and end - start <= SHORT_STAY_MAX:
                continue
            event = base_event_draft(
                # 일정 event 가 체류 일부만 덮었으면 나머지는 일정이 아니라 체류다.
                base if whole or base.stay is None else base.stay,
                start=start,
                end=end,
                places=bundle.places,
                client_event_id=f"base-{base.base_id}-{len(restored) + 1}",
            )
            draft.events.append(event)
            restored.append(event.title)

    draft.warnings = [w for w in draft.warnings if not w.warning_id.startswith(_WARNING_ID)]
    if restored:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_ID}-001",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"어느 event 도 덮지 않은 기본 event 의 시간 {len(restored)}곳을 틀 문장 "
                    f"event 로 되살렸습니다: {', '.join(restored[:3])}"
                ),
            )
        )
        logger.debug("위치 구간 복원: %d건", len(restored))


def _uncovered(start, end, covering):
    """[start, end] 에서 covering 구간들이 덮지 않은 조각."""

    pieces = []
    cursor = start
    for left, right in sorted((l, r) for l, r in covering if r > l):
        if left > cursor:
            pieces.append((cursor, min(left, end)))
        cursor = max(cursor, right)
        if cursor >= end:
            break
    if cursor < end:
        pieces.append((cursor, end))
    return [(left, right) for left, right in pieces if right > left]


#: 교통 결제·예약은 이동 기록이 없어도 이동의 근거다(Timeline v4 `MOVEMENT` 줄).
_MOVEMENT_NOTIFICATION_KINDS = frozenset({NotificationClueKind.PAYMENT, NotificationClueKind.RESERVATION})


def remove_unrecorded_movements(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """이동 기록도 교통 결제·예약도 없는 `MOVEMENT` event 를 지운다(in-place).

    빈 시간(기록되지 않은 이동)은 event 가 아니다. 프롬프트가 그렇게 말해도 실제 Timeline 은
    `기록 없는 이동 구간이라 별도 이동으로 두었다` 며 앞뒤 체류 묶음을 근거로 이동 event 를
    만들었고, 그 체류를 가져가 버려 체류가 하루에서 사라졌다. 규칙으로 정해지는 일이라 코드가
    지운다. 그 근거였던 체류는 다음 단계(`ensure_skeleton_segments`)가 되살린다.
    """

    movement_ids = {raw_id_of(item) for item in request.movements}
    transport_ids = {
        raw_id
        for clue in clues.notifications
        if clue.kind in _MOVEMENT_NOTIFICATION_KINDS
        for raw_id in clue.raw_ids
    }
    removed = [
        event
        for event in draft.events
        if event.event_type is EventType.MOVEMENT
        and not _has_calendar(event)
        and not any(ref.raw_id in movement_ids | transport_ids for ref in event.source_refs)
    ]
    if not removed:
        return
    draft.events = [event for event in draft.events if event not in removed]
    logger.debug("이동 기록 없는 이동 event %d건을 지웠습니다.", len(removed))


def separate_movement_segments(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """이동 구간의 기록은 이동·산책 event 에만 둔다(in-place).

    이동은 예외 없이 나눈다(#134, 사용자 결정). v4 는 뼈대가 이동 구간을 정확히 알므로 코드가
    지킨다 — 실제 Timeline 은 `B1의 체류 뒤 B2의 이동과 사무실 작업 사진이 이어져 오전의
    작업 흐름으로 묶었다` 며 집 체류와 출근 이동을 한 `WORK` 로 합쳤다. 다른 event 에서 이동
    기록을 빼고, 그 event 의 시간이 이동 구간에 걸쳐 있으면 걸친 끝을 이동 구간 밖으로 자른다.
    빠진 이동 구간은 `ensure_skeleton_segments` 가 이동 event 로 되살린다.

    이동 구간을 통째로 품은 event 도 같다 — 실제 Timeline 은 40km 떨어진 집과 사무실 체류를
    `이어진다` 며 06:00~22:00 하나로 합쳤다. 그 event 는 이동 앞뒤 중 위치 근거가 더 긴 쪽만
    남기고, 다른 쪽의 체류 근거와 기본 event 를 뺀다. 뺀 쪽은 되살리는 단계가 틀 문장으로 덮는다.

    이동 기록이 없어도 **다른 장소는 한 event 에 두지 않는다**(사용자 결정 2026-10-30, "위치가
    서로 다르면 무조건 … 끊어내야"). 장소가 같은지는 체류 구간의 주소로 본다 — 코드가 250m
    묶음마다 하나로 맞춘 주소라 장소 이름 후보와 달리 판단 기준이 된다. 주소가 다른 체류 구간을
    함께 가진 event 는 그 event 시간과 가장 오래 겹치는 장소만 남긴다(`_keep_one_place`).
    """

    movement_segments = {
        raw_id: segment
        for segment in clues.skeleton.segments
        if segment.kind is not SegmentKind.STAY
        for raw_id in segment.raw_ids
    }
    base_segments = {
        bundle.base_event.base_id: bundle.base_event.segment
        for bundle in clues.bundles
        if bundle.base_event
    }
    if not movement_segments:
        _keep_one_place(draft, clues, base_segments)
        return
    movement_bases = {
        bundle.base_event.base_id
        for bundle in clues.bundles
        if bundle.base_event and bundle.base_event.segment.kind is not SegmentKind.STAY
    }
    tz = resolve_timezone(request.timezone)
    spans = _source_spans(request, tz)
    kept: list[TimelineEventDraft] = []
    for event in draft.events:
        if event.event_type in (EventType.MOVEMENT, EventType.EXERCISE):
            kept.append(event)
            continue
        # 일정 event 의 시간은 바꾸지 않는다(사용자 결정) — 이동 기록만 빼고 자르지 않는다.
        calendar_event = _has_calendar(event)
        for segment in {s.segment_id: s for s in movement_segments.values()}.values():
            if not calendar_event and event.start_time < segment.start and segment.end < event.end_time:
                _keep_one_side(event, segment, spans, base_segments)
        hits = {
            movement_segments[ref.raw_id].segment_id: movement_segments[ref.raw_id]
            for ref in event.source_refs
            if ref.raw_id in movement_segments
        }
        if not hits:
            kept.append(event)
            continue
        event.source_refs = [ref for ref in event.source_refs if ref.raw_id not in movement_segments]
        event.base_event_ids = [b for b in event.base_event_ids if b not in movement_bases]
        if not event.source_refs:
            continue
        if calendar_event:
            kept.append(event)
            continue
        had_length = event.end_time > event.start_time
        for segment in hits.values():
            # Timeline 의 시각은 분 단위라 기록의 초(10:19:54)와 어긋난다. 1분 안은 같은 경계다.
            if segment.start - _MINUTE <= event.start_time < segment.end:
                event.start_time = segment.end
            if segment.start < event.end_time <= segment.end + _MINUTE:
                event.end_time = segment.start
        # 시간이 통째로 이동 구간이던 event 는 남는 시간이 없다. 이동을 다른 활동으로 채운 것이라
        # 지운다 — 실제 Timeline 은 출근 이동을 `업무` 로 채웠고, 자르고 나니 길이 0인 업무가 남았다.
        if had_length and event.end_time <= event.start_time:
            continue
        if event.end_time < event.start_time:
            event.end_time = event.start_time
        kept.append(event)
    draft.events = kept
    _keep_one_place(draft, clues, base_segments)


def _keep_one_place(draft: TimelineDraft, clues: TimelineClues, base_segments) -> None:
    """주소가 다른 체류 구간을 함께 가진 event 를 한 장소로 줄인다(in-place).

    남기는 장소는 event 시간과 가장 오래 겹치는 장소이고, 같으면 `from` 에 먼저 적힌 쪽이다.
    다른 장소의 체류 근거와 기본 event 를 빼고, 그 장소 쪽으로 걸친 끝을 남기는 장소의 구간까지
    자른다. 뺀 장소는 `ensure_skeleton_segments` 가 틀 문장으로 되살린다.

    일정 event 는 시간을 자르지 않는다(사용자 결정) — 다른 장소의 근거만 뺀다. "일정을 그렇게
    등록해놔도 잠깐 바깥에 나가서 카페에 있을 수도 있고 그렇기 때문에 위치 뼈대는 살리는 게 맞다."
    """

    stay_segments = {
        raw_id: segment
        for segment in clues.skeleton.segments
        if segment.kind is SegmentKind.STAY
        for raw_id in segment.raw_ids
    }
    if not stay_segments:
        return

    def place_of(segment) -> str:
        return clues.address(segment.address) or segment.segment_id

    for event in draft.events:
        if event.event_type in (EventType.MOVEMENT, EventType.EXERCISE):
            continue
        touched = {
            segment.segment_id: segment
            for segment in (
                *(stay_segments.get(ref.raw_id) for ref in event.source_refs),
                *(base_segments.get(base) for base in event.base_event_ids),
            )
            if segment is not None and segment.kind is SegmentKind.STAY
        }
        places: dict[str, list] = {}
        for segment in touched.values():
            places.setdefault(place_of(segment), []).append(segment)
        if len(places) < 2:
            continue

        def overlap(segments) -> timedelta:
            return sum(
                (
                    max(min(event.end_time, s.end) - max(event.start_time, s.start), timedelta(0))
                    for s in segments
                ),
                timedelta(0),
            )

        first_base = next(
            (place_of(seg) for base in event.base_event_ids if (seg := base_segments.get(base))),
            None,
        )
        keep = max(places, key=lambda place: (overlap(places[place]), place == first_base))
        dropped = [segment for place, group in places.items() if place != keep for segment in group]
        dropped_ids = {raw_id for segment in dropped for raw_id in segment.raw_ids}
        dropped_segments = {segment.segment_id for segment in dropped}
        event.source_refs = [ref for ref in event.source_refs if ref.raw_id not in dropped_ids]
        event.base_event_ids = [
            base
            for base in event.base_event_ids
            if not ((seg := base_segments.get(base)) and seg.segment_id in dropped_segments)
        ]
        if _has_calendar(event):
            continue
        keep_start = min(segment.start for segment in places[keep])
        keep_end = max(segment.end for segment in places[keep])
        if any(segment.end <= keep_start for segment in dropped):
            event.start_time = max(event.start_time, keep_start)
        if any(segment.start >= keep_end for segment in dropped):
            event.end_time = min(event.end_time, keep_end)
        if event.end_time < event.start_time:
            event.end_time = event.start_time


def spares_from_sleep(clues: TimelineClues) -> Callable[[TimelineEventDraft, TimelineEventDraft], bool]:
    """수면 event 와 겹쳐도 남길 event 를 고르는 함수(v4, `remove_events_overlapping_sleep`).

    수면 일정은 계획이고 위치는 기록이다. 실제 재실행에서 수면 일정(00:30~08:30)과 21분 겹친다고
    08:09 에 출발한 이동이 지워졌다. 이동 구간을 가진 event 와, 수면 장소와 주소가 다른 체류를 가진
    event 는 남긴다(사용자 결정 2026-10-30: 일정은 그대로, 위치 뼈대도 살린다). 수면 장소의 체류는
    예전처럼 수면이 설명하는 시간이라 지운다.
    """

    segments = {raw_id: segment for segment in clues.skeleton.segments for raw_id in segment.raw_ids}

    def place_of(segment) -> str:
        return clues.address(segment.address) or segment.segment_id

    def sleep_places(sleep: TimelineEventDraft) -> set[str]:
        held = {
            place_of(segments[ref.raw_id])
            for ref in sleep.source_refs
            if ref.raw_id in segments and segments[ref.raw_id].kind is SegmentKind.STAY
        }
        if held:
            return held
        overlapping = [
            (min(sleep.end_time, s.end) - max(sleep.start_time, s.start), s)
            for s in clues.skeleton.segments
            if s.kind is SegmentKind.STAY and s.start < sleep.end_time and sleep.start_time < s.end
        ]
        return {place_of(max(overlapping, key=lambda pair: pair[0])[1])} if overlapping else set()

    def spare(event: TimelineEventDraft, sleep: TimelineEventDraft) -> bool:
        held = [segments[ref.raw_id] for ref in event.source_refs if ref.raw_id in segments]
        if any(segment.kind is not SegmentKind.STAY for segment in held):
            return True
        stays = {place_of(segment) for segment in held}
        return bool(stays - sleep_places(sleep))

    return spare


def _keep_one_side(event: TimelineEventDraft, segment, spans, base_segments) -> None:
    """이동 구간을 품은 event 를 이동 앞뒤 중 위치 근거가 더 긴 쪽으로 줄인다."""

    def stay_time(left, right) -> timedelta:
        total = timedelta(0)
        for ref in event.source_refs:
            span = spans.get(ref.raw_id)
            if ref.source_type is EventSourceType.STAY and span and span[0] and span[1]:
                total += max(min(span[1], right) - max(span[0], left), timedelta(0))
        return total

    keep_after = stay_time(segment.end, event.end_time) >= stay_time(event.start_time, segment.start)

    def other_side(start, end) -> bool:
        return end <= segment.start if keep_after else start >= segment.end

    if keep_after:
        event.start_time = segment.end
    else:
        event.end_time = segment.start
    event.source_refs = [
        ref
        for ref in event.source_refs
        if not (
            ref.source_type is EventSourceType.STAY
            and (span := spans.get(ref.raw_id))
            and span[0]
            and span[1]
            and other_side(*span)
        )
    ]
    event.base_event_ids = [
        base
        for base in event.base_event_ids
        if not ((seg := base_segments.get(base)) and other_side(seg.start, seg.end))
    ]


def _has_calendar(event: TimelineEventDraft) -> bool:
    return any(ref.source_type is EventSourceType.CALENDAR for ref in event.source_refs)


def remove_calendar_events_without_calendar(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """일정 근거가 없는 `CALENDAR_EVENT` 를 정리한다(in-place).

    일정 event 는 사용자가 캘린더에 적은 일정이 있을 때만이다(사용자 결정). 실제 Timeline 은
    Gmail·문자로 온 예비소집 안내를 `11:25~23:59 ICPC 예비소집 안내` 일정 event 로 만들었다.
    대화가 아닌 알림은 시각을 말하지 않아(사용자 결정) 알림만 근거인 일정 event 는 시간이 지어낸
    것이라 지운다. 다른 근거가 있으면 그 근거의 event 로 두고 타입만 바꾼다 — 근거가 위치뿐이면 `STAY`,
    사진·알림이 함께 있으면 `UNKNOWN` 이다.
    """

    notification_ids = {raw_id_of(item) for item in request.notifications}
    location_ids = {raw_id_of(item) for item in [*request.stays, *request.movements]}
    kept: list[TimelineEventDraft] = []
    for event in draft.events:
        if event.event_type is EventType.CALENDAR_EVENT and not _has_calendar(event):
            if all(ref.raw_id in notification_ids for ref in event.source_refs):
                continue
            only_location = all(ref.raw_id in location_ids for ref in event.source_refs)
            event.event_type = EventType.STAY if only_location else EventType.UNKNOWN
            # `…일정이 있었다` 문장은 일정이 없으면 틀린 말이다. 종류와 글은 Repair 가 근거로 쓴다.
            event.merge_reason = NEEDS_WRITING
        kept.append(event)
    draft.events = kept


#: 체류를 말하는 타입. 식사의 `먹으며 시간을 보냈다` 처럼 다른 타입도 같은 말을 쓰므로 타입으로 가른다.
_STAY_TYPES = frozenset({EventType.STAY, EventType.UNKNOWN, EventType.REST, EventType.SLEEP})

#: 체류를 말하는 문장 뼈대(「문장 형식」 표). 위치 근거 없이 이렇게 쓰면 있지도 않은 체류다.
_STAY_SENTENCE = re.compile(r"(시간을 보냈다|밤을 보냈다|잠을 잤다)")
_STAY_TITLE = re.compile(r"(보낸 시간|보낸 밤|머문 시간|에서 잠)$")


def remove_stays_without_location(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """위치 근거도 일정도 없이 체류·밤을 말하는 event 를 지운다(in-place).

    실제 Timeline 은 22시에 위치 기록이 끊긴 뒤를 `마지막 체류 이후 기록이 없어 도화동에서 이어진
    밤으로 묶었다` 며 `도화동에서 보낸 밤` 으로 만들고, 그 시간에 찍은 저녁 사진 두 장과 대화를
    근거로 댔다(사용자: 절대 나와서는 안 되는 문제). 체류 문장은 체류 기록이 있어야 한다. 사진은
    사진 단일 귀속이 그 시각의 사진 event 로 되살리고, Repair 가 사진 해석으로 다시 쓴다.
    """

    location_ids = {raw_id_of(item) for item in [*request.stays, *request.movements]}
    draft.events = [
        event
        for event in draft.events
        if event.event_type not in _STAY_TYPES
        or _has_calendar(event)
        or any(ref.raw_id in location_ids for ref in event.source_refs)
        or not (
            _STAY_SENTENCE.search(event.description or "") or _STAY_TITLE.search(event.title)
        )
    ]


def remove_events_outside_evidence(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> None:
    """event 를 자기 근거의 시간에 맞춘다(in-place).

    event 는 근거의 시간에 있었던 일이다. 사진·알림처럼 한 시점인 근거는 event 시간 안(앞뒤
    20분 여유)에 있으면 되고, 체류·이동·일정처럼 구간인 근거는 event 시간과 충분히 겹쳐야 한다
    (event 길이의 절반 또는 30분 중 짧은 쪽). 경계에서 몇 초 닿은 것은 겹친 것이 아니다.
    근거와 겹치지 않는 event 는 지운다.

    그리고 event 를 근거가 덮는 시간 밖으로 늘이지 않는다 — 벗어난 끝을 자르고, 자르고 나서
    남는 시간이 없으면 지운다. 실제 Timeline 은 위치 기록이 끊긴 22시 뒤를 저녁 사진 두 장으로,
    또 사무실 체류를 근거로 대고 `밤을 보냈다` 8시간짜리 event 를 만들었다.

    - 위치 근거가 있는 event 의 시간은 위치 기록·사진·일정이 덮는 시간이다. 그 자리에 있었다고
      말하는 근거들이고, 휴대폰으로 오간 메시지는 그곳에 있었다는 근거가 아니다.
    - 위치 근거가 없는 event(대화·사진으로 만든 event)는 대화 알림의 시각도 시간을 덮는다.
    - 대화가 아닌 알림은 시각을 말하지 않는다(사용자 결정) — 받은 시각은 그 일이 일어난 때가
      아니다. 그런 알림만 근거인 event 는 자르지 않는다.
    """

    tz = resolve_timezone(request.timezone)
    spans = _source_spans(request, tz)
    location_ids = {raw_id_of(item) for item in [*request.stays, *request.movements]}
    notification_ids = {raw_id_of(item) for item in request.notifications}
    conversation_ids = {
        raw_id
        for clue in clues.notifications
        if clue.kind is NotificationClueKind.CONVERSATION
        for raw_id in clue.raw_ids
    }
    photo_ids = {raw_id_of(item) for item in request.photos}
    activity_ids = photo_ids | {raw_id_of(item) for item in request.calendars} | {
        raw_id
        for clue in clues.notifications
        if clue.kind in _MOVEMENT_NOTIFICATION_KINDS
        for raw_id in clue.raw_ids
    }
    kept: list[TimelineEventDraft] = []
    for event in draft.events:
        if not _supported(event, [spans.get(ref.raw_id) for ref in event.source_refs]):
            continue
        _detach_distant_photos(event, spans, photo_ids, activity_ids, clues)
        located = any(ref.raw_id in location_ids for ref in event.source_refs)
        reach = [
            spans.get(ref.raw_id)
            for ref in event.source_refs
            if ref.raw_id not in notification_ids
            or (not located and ref.raw_id in conversation_ids)
        ]
        had_length = event.end_time > event.start_time
        _fit_to(event, reach)
        if had_length and event.end_time <= event.start_time:
            continue
        kept.append(event)
    draft.events = kept


def _detach_distant_photos(
    event: TimelineEventDraft, spans, photo_ids: set[str], activity_ids: set[str], clues: TimelineClues
) -> None:
    """촬영 시각이 event 시간(앞뒤 20분) 밖인 사진을 그 event 에서 뺀다.

    실제 Timeline 은 주소 없는 오전 11시 사진을 다섯 시간 뒤의 체류에 붙여 `덕충동에서 보수 작업`
    을 만들었다. 뺀 사진은 사진 단일 귀속이 촬영 시각의 event 로 되살린다. 활동 근거가 남지 않으면
    남은 문장은 뺀 사진이 말한 활동이라 Repair 가 다시 쓴다(`NEEDS_WRITING`).
    """

    distant = [
        ref
        for ref in event.source_refs
        if ref.raw_id in photo_ids
        and (span := spans.get(ref.raw_id)) is not None
        and span[0] is not None
        and not event.start_time - SHORT_STAY_MAX <= span[0] <= event.end_time + SHORT_STAY_MAX
    ]
    if not distant or len(distant) == len(event.source_refs):
        return
    event.source_refs = [ref for ref in event.source_refs if ref not in distant]
    if not any(ref.raw_id in activity_ids for ref in event.source_refs):
        event.merge_reason = NEEDS_WRITING


def _back_to_frame(event: TimelineEventDraft, clues: TimelineClues) -> None:
    """event 를 온 곳의 체류 틀 문장으로 되돌린다."""

    base = next(
        (bundle.base_event for b in event.base_event_ids if (bundle := clues.base_bundle(b)) and bundle.base_event),
        None,
    )
    if base is None:
        return
    frame = base.stay if base.stay is not None else base
    event.event_type = frame.event_type
    event.title = frame.title
    event.description = frame.description


def _fit_to(event: TimelineEventDraft, spans) -> None:
    """event 의 끝을 근거가 덮는 시간(시점은 앞뒤 20분) 안으로 자른다."""

    reach = [
        (start - SHORT_STAY_MAX, end + SHORT_STAY_MAX) if start == end else (start, end)
        for span in spans
        if span is not None and (start := span[0]) is not None and (end := span[1]) is not None
    ]
    if not reach:
        return
    first = min(left for left, _ in reach)
    last = max(right for _, right in reach)
    if event.start_time < first:
        event.start_time = first
    if event.end_time > last:
        event.end_time = last
    if event.end_time < event.start_time:
        event.end_time = event.start_time


def _supported(event: TimelineEventDraft, spans) -> bool:
    duration = event.end_time - event.start_time
    needed = min(duration / 2, _MIN_OVERLAP)
    for span in spans:
        if span is None or span[0] is None or span[1] is None:
            continue
        start, end = span
        if start == end:
            if event.start_time - SHORT_STAY_MAX <= start <= event.end_time + SHORT_STAY_MAX:
                return True
            continue
        overlap = min(end, event.end_time) - max(start, event.start_time)
        if duration == timedelta(0):
            if start <= event.start_time <= end:
                return True
        elif overlap > timedelta(0) and overlap >= needed:
            return True
    return False


def _source_spans(request: TimelineDraftRequest, tz) -> dict:
    spans = {}
    for item in [*request.stays, *request.movements, *request.calendars]:
        start = parse_datetime(item.start_at, tz)
        end = parse_datetime(item.end_at, tz) if item.end_at else start
        spans[raw_id_of(item)] = (start, end)
    for photo in request.photos:
        taken = parse_datetime(photo.taken_at, tz)
        spans[raw_id_of(photo)] = (taken, taken)
    for notification in request.notifications:
        posted = parse_datetime(notification.posted_at, tz)
        spans[raw_id_of(notification)] = (posted, posted)
    return spans
