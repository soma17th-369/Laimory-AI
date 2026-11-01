"""Repair Agent 도구 카탈로그.

Repair Agent 는 draft 를 직접 다시 쓰지 않는다. **이미 있는 결정론 서비스와 Agent 를
도구로 호출**해서 고친다. 도구는 세 종류다.

    1. 조회   : `lookup_source` 로 근거 원본을 되짚는다.
    2. 편집   : `update_event` / `delete_event` / `split_event` / `absorb_location_event`
                로 event 를 고치거나 지우거나 나누거나 체류 카드를 흡수한다.
    3. 재적용 : `enforce_sleep_boundary`, `resolve_places` 같은 결정론 서비스를 다시 돌린다.
    4. 재실행 : `rerun_event_agent` / `rerun_timeline_agent` 로 상류 Agent 를 다시 돌린다.

3번이 도구인 이유: 이 서비스들은 `repair_draft` 가 이미 한 번 돌린 것들이다. 하지만
Repair Agent 가 event 를 고치거나 상류 Agent 를 다시 돌리면 그 확정이 무너지므로,
LLM 이 "무엇을 다시 확정해야 하는지" 골라 부를 수 있어야 한다. 로직을 복제하지 않고
같은 함수를 그대로 부른다.

**정렬·`clientEventId` 부여·window 강제는 도구가 아니다.** 그 셋은 결과가 반드시
일관돼야 하는 처리라 LLM 의 선택지로 두지 않는다. Repair Agent 는 매 반복이 끝날 때
`repair_draft` 를 통째로 다시 돌리고, 그 안에서 코드가 항상 확정한다.

도구 실행 실패는 예외로 새지 않고 `RepairToolResult(ok=False)` 로 돌아가 다음 분석
프롬프트에 실린다. LLM 이 자기가 뭘 잘못 불렀는지 보고 고쳐 부를 수 있게 하기 위해서다.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from time import perf_counter

from app.agents.events import merge_event_results
from app.agents.parsing import strip_prompt_excluded_keys
from app.agents.events.base_event_agent import EventAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.logging import get_logger
from app.core.langfuse_tracing import (
    token_usage_scope,
    trace_observation,
    update_observation,
)
from app.core.execution_context import ExecutionStage, execution_scope
from app.schemas import (
    AgentEventResult,
    EventSourceType,
    EventType,
    PRE_CLUE_EVENT_TYPES,
    HealthMetric,
    RepairToolCall,
    RepairToolResult,
    SourceRef,
    TimelineDraft,
    TimelineDraftRequest,
)
from app.schemas.clues import NotificationClueKind
from app.services.calendar_guard import ensure_calendar_events
from app.services.clue_bundles import (
    TimelineClues,
    expand_evidence,
    misplaced_clues,
    timeline_input_sections,
)
from app.services.calendar_location import reinforce_calendar_location
from app.services.confirm_report import ConfirmReport, reports_to_prompt
from app.services.draft_edit import (
    absorb_location_event,
    delete_event,
    split_event,
    update_event,
)
from app.services.draft_repair import (
    align_location_events,
    merge_stay_events,
    repair_durations,
    resolve_overlaps,
    sort_events,
)
from app.services.meal_guard import (
    MEAL_STAY_MAX_DURATION,
    enforce_meal_duration,
    follows_calendar,
)
from app.services.skeleton_guard import NEEDS_WRITING
from app.services.photo_guard import inspect_photo_assignment
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError, code_of, report_error
from app.services.place_resolver import resolve_places
from app.services.place_slots import confirm_place_slots
from app.services.sleep_guard import enforce_sleep_boundary
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

#: v1~v3 세트의 도구 인자가 쓸 수 없는 eventType 문자열. 모르는 문자열은 `draft_edit` 가 거절한다.
_NOT_IN_PRE_CLUE_SETS = frozenset(member.value for member in set(EventType) - PRE_CLUE_EVENT_TYPES)


class RepairToolError(AppError):
    """도구를 실행할 수 없을 때(인자 오류·없는 대상 등)."""

    default_code = ErrorCode.REPAIR_TOOL_FAILED


@dataclass
class RepairContext:
    """도구들이 함께 보고 고치는 상태.

    `draft` 는 도구가 통째로 바꿀 수 있어(`rerun_timeline_agent`) 컨텍스트가 들고
    있는다. `event_results` 는 Agent 이름 → 그 Agent 의 마지막 결과이며,
    `rerun_event_agent` 가 해당 항목만 갈아 끼운다. 그래야 Timeline Agent 를 다시
    돌릴 때 **다시 돌린 Agent 의 결과만 새것**이고 나머지는 처음 결과 그대로다.
    """

    request: TimelineDraftRequest
    draft: TimelineDraft
    event_results: dict[str, AgentEventResult] = field(default_factory=dict)
    event_agents: dict[str, EventAgent] = field(default_factory=dict)
    timeline_agent: TimelineAgent | None = None
    log: list[RepairToolResult] = field(default_factory=list)
    #: 확정된 draft 를 밖으로 내보내는 콜백(이슈 #76). `_confirm` 만 호출한다.
    #: 호출자는 제한 시간이 끝나 이 실행이 취소돼도 마지막 확정본을 손에 남긴다.
    on_confirm: Callable[[TimelineDraft], None] | None = None
    #: 확정할 때마다 하나씩 쌓이는 보정 기록(이슈 #119). `_confirm` 만 더한다.
    #: draft 가 아니라 여기 있으므로 결과 저장 계약에는 나가지 않는다.
    reports: list[ConfirmReport] = field(default_factory=list)
    #: v3 세트의 입력과 도구를 쓰는가(이슈 #119). 거짓이면 Repair 가 보는 입력과 도구가
    #: 예전 그대로다. 값은 Repair Agent 가 프롬프트 세트를 보고 정한다.
    extended: bool = False
    #: 지금까지 준 `clientEventId` 의 장부(이슈 #144). `_confirm` 이 `repair_draft` 에 넘긴다.
    #: 한 번 준 번호는 Repair 가 끝날 때까지 그 event 를 가리키고, 지운 번호는 다시 쓰지
    #: 않는다. 도구 로그와 쌓인 보정 기록은 실행 당시의 번호를 그대로 싣기 때문이다.
    issued_ids: set[str] = field(default_factory=set)
    #: v4 세트의 근거 묶음(#169). 있으면 Repair 는 v4 계약으로 돈다 — 입력은 묶음·단서이고,
    #: 도구 인자의 근거는 단서 id 도 받으며, 재실행은 같은 묶음으로 Timeline 만 다시 돌린다.
    clues: TimelineClues | None = None


@dataclass(frozen=True)
class RepairTool:
    """도구 하나. `usage` 는 그대로 프롬프트의 도구 카탈로그에 실린다."""

    name: str
    usage: str
    description: str
    run: Callable[[RepairContext, dict], str]
    #: v3 세트에서만 내놓는 도구인가.
    #:
    #: v2 Repair 프롬프트는 이 도구를 언제 쓰는지 모른다. `split_event` 를 v2 에 내놓고
    #: 실제 LLM 으로 돌렸더니 캘린더 일정대로인 event 와 사진 event 를 잘게 쪼개 event 가
    #: 7개에서 13개로 늘었다. v2 는 운영 세트라 프롬프트를 고치지 않으므로 도구를 주지 않는다.
    extended_only: bool = False


# --- 조회 --------------------------------------------------------------------

#: HEALTH 는 하나의 itemType 이지만 metric 으로 SLEEP/ACTIVITY 를 나눈다.
#: 문장이 식사를 말하는 표현.
_MEAL_WORDS = re.compile(r"먹|식사")

_HEALTH_SOURCE_TYPES = {
    HealthMetric.SLEEP: EventSourceType.SLEEP,
    HealthMetric.STEPS: EventSourceType.ACTIVITY,
}


def _source_index(request: TimelineDraftRequest) -> dict[str, tuple[EventSourceType, object]]:
    """식별자(rawId) → (sourceType, 입력 항목)."""

    index: dict[str, tuple[EventSourceType, object]] = {}

    grouped: list[tuple[EventSourceType, list]] = [
        (EventSourceType.STAY, list(request.stays)),
        (EventSourceType.MOVEMENT, list(request.movements)),
        (EventSourceType.CALENDAR, list(request.calendars)),
        (EventSourceType.NOTIFICATION, list(request.notifications)),
        (EventSourceType.PHOTO, list(request.photos)),
    ]
    for source_type, items in grouped:
        for item in items:
            if identifier := raw_id_of(item):
                index[identifier] = (source_type, item)

    for item in request.healths:
        identifier = raw_id_of(item)
        source_type = _HEALTH_SOURCE_TYPES.get(item.metric)
        if identifier and source_type is not None:
            index[identifier] = (source_type, item)

    return index


def _item_label(item) -> str:
    """입력 항목 한 줄 요약용 라벨."""

    for attribute in ("title", "place", "app_name", "filename", "metric"):
        value = getattr(item, attribute, None)
        if value:
            return str(getattr(value, "value", value))
    return ""


def source_index_text(
    request: TimelineDraftRequest, *, hidden: frozenset[str] | set[str] = frozenset()
) -> str:
    """근거 원본 목록을 한 줄씩 요약한다(프롬프트용).

    전체 항목을 통째로 싣지 않는 이유는 프롬프트가 비대해지기 때문이다. 자세한 값이
    필요하면 LLM 이 `lookup_source` 로 그 항목만 꺼내 본다. `hidden` 은 싣지 않을 rawId 다.
    """

    lines: list[str] = []
    for raw_id, (source_type, item) in _source_index(request).items():
        if raw_id in hidden:
            continue
        start = getattr(item, "start_at", None) or getattr(item, "taken_at", None) or getattr(item, "posted_at", None) or ""
        end = getattr(item, "end_at", None) or ""
        span = f"{start}~{end}" if end else str(start)
        label = _item_label(item)
        lines.append(f"- {source_type.value} rawId={raw_id} {span} {label}".rstrip())
    return "\n".join(lines) if lines else "없음"


def dropped_notification_ids(ctx: RepairContext) -> set[str]:
    """Notification v4 가 단서로 남기지 않은 알림의 rawId. Repair 입력에 싣지 않는다(#173)."""

    if ctx.clues is None:
        return set()
    kept = {raw_id for clue in ctx.clues.notifications for raw_id in clue.raw_ids}
    return {raw_id_of(item) for item in ctx.request.notifications} - kept


def _lookup_source(ctx: RepairContext, args: dict) -> str:
    raw_id = args.get("rawId")
    if not raw_id:
        raise RepairToolError("rawId 인자가 필요합니다.")

    found = _source_index(ctx.request).get(str(raw_id))
    if found is None:
        raise RepairToolError(
            f"rawId '{raw_id}' 인 입력 항목이 없습니다. 근거로 인용할 수 없는 값입니다."
        )
    source_type, item = found
    # 좌표(#80)와 `photoUrl`(#127)은 프롬프트에 싣지 않는다. Repair Agent 는 좌표로 판단할
    # 일이 없고, 사진 URL 은 내려받아야 의미가 있어 LLM 에게 정보가 아니다.
    payload = strip_prompt_excluded_keys(item.model_dump(by_alias=True, mode="json"))
    return f"{source_type.value} {json.dumps(payload, ensure_ascii=False)}"


# --- 편집 --------------------------------------------------------------------


def _require_type_of_the_set(ctx: RepairContext, event_type: object) -> None:
    """v1~v3 세트에 없는 타입을 거절한다(#172).

    도구 인자는 자유 dict 이고 draft 는 공용 enum 이라, 거절하지 않으면 새 값이 App Server 까지
    간다. `STAY` 는 v3 의 sourceType 과 같은 글자라 모델이 쓸 수 있는 값이다.
    """

    if ctx.clues is None and isinstance(event_type, str) and event_type in _NOT_IN_PRE_CLUE_SETS:
        raise RepairToolError(f"이 프롬프트 세트에 없는 eventType 입니다: {event_type}")


def _update_event(ctx: RepairContext, args: dict) -> str:
    client_event_id = args.get("clientEventId")
    if not client_event_id:
        raise RepairToolError("clientEventId 인자가 필요합니다.")

    fields = args.get("fields")
    if not isinstance(fields, dict):
        raise RepairToolError("fields 인자(바꿀 필드만 담은 객체)가 필요합니다.")
    _require_type_of_the_set(ctx, fields.get("eventType"))
    named_photos: set[str] = set()
    if "evidence" in fields:
        identifiers = fields["evidence"]
        fields = _with_expanded_evidence(ctx, fields, str(client_event_id))
        named_photos = _named_photos(ctx, identifiers)
        fields = _without_misplaced(ctx, str(client_event_id), fields, named_photos)

    if ctx.clues is not None:
        becomes_meal = fields.get("eventType") == EventType.MEAL.value
        if becomes_meal or _writes_a_meal(ctx, str(client_event_id), fields):
            _require_meal_evidence(ctx, str(client_event_id), fields, becomes_meal=becomes_meal)
    event = update_event(ctx.draft, str(client_event_id), fields)
    # 제목만 고치면 문장이 빈 채로 남는다. 문장까지 있어야 다 쓴 것이다.
    if event.merge_reason == NEEDS_WRITING and "description" in fields and event.description.strip():
        event.merge_reason = None
    # Repair 가 사진을 직접 적은 event 가 그 사진의 주인이다(사용자 결정 2026-10-30 — 코드는 Repair 를
    # 따른다). 다른 event 에서 빼 두면 확정 pass 의 사진 단일 귀속이 자기 순서로 Repair 의 선택을 뒤집지
    # 않는다. 회의·식사 실행에서 그 순서가 일정이 있는 회의를 골라 식사가 지워졌다.
    for other in ctx.draft.events:
        if other is not event:
            other.source_refs = [ref for ref in other.source_refs if ref.raw_id not in named_photos]
    return f"{client_event_id} 를 수정했습니다: {event.title} ({', '.join(sorted(fields))})"


def _writes_a_meal(ctx: RepairContext, client_event_id: str, fields: dict) -> bool:
    """타입을 두고 문장만 식사로 고치는 호출인가. 식사 약속처럼 일정이 있는 event 는 일정의 말이다."""

    current = next((e for e in ctx.draft.events if e.client_event_id == client_event_id), None)
    if current is None or any(ref.source_type is EventSourceType.CALENDAR for ref in current.source_refs):
        return False
    return bool(_MEAL_WORDS.search(f"{fields.get('title') or ''} {fields.get('description') or ''}"))


def _require_meal_evidence(
    ctx: RepairContext, client_event_id: str, fields: dict, *, becomes_meal: bool = True
) -> None:
    """근거 없이 식사를 쓰지 않는다(v4, 사용자 결정 2026-10-31).

    식사는 시간이 맞는 사진이나 알림(결제)이 있어야 하고 3시간을 넘지 않는다(식당에 머문 식사 상한).
    Repair 가 없는 사진을 근거로("B3 안에 점심처럼 보이는 사진") 아침 체류를 `점심 식사` 로, 검사 결과를
    따라 7시간 체류를 식사로 바꿨다 — 둘 다 근거는 그대로 두고 eventType·문장만 바꿨다. 타입을 두고 문장만
    식사로 고치는 호출도 같은 근거를 본다 — 대화 알림만 가진 아침 체류의 문장이 식사로 덮였다. 다만 긴
    체류에 사진이 있으면 `점심을 먹으며 시간을 보냈다` 로 쓸 수 있어 길이는 보지 않는다(사용자 결정).
    """

    current = next((e for e in ctx.draft.events if e.client_event_id == client_event_id), None)
    if current is None:
        return  # 없는 event 는 update_event 가 거절한다
    tz = resolve_timezone(ctx.request.timezone)
    start = parse_datetime(fields["startTime"], tz) if fields.get("startTime") else current.start_time
    end = parse_datetime(fields["endTime"], tz) if fields.get("endTime") else current.end_time
    refs = (
        [SourceRef.model_validate(ref) for ref in fields["sourceRefs"]]
        if "sourceRefs" in fields
        else current.source_refs
    )
    _require_meal_evidence_of(ctx, start, end, refs, check_length=becomes_meal)


def _require_meal_evidence_of(
    ctx: RepairContext, start, end, refs: list[SourceRef], *, check_length: bool = True
) -> None:
    if check_length and end - start > MEAL_STAY_MAX_DURATION:
        raise RepairToolError(
            "3시간이 넘는 event 는 식사가 아닙니다. 그 안의 식사는 split_event 로 따로 꺼냅니다."
        )
    # 식사 약속 일정 그대로인 event 는 일정이 근거다(#184). 나눈 조각은 일정 시간과 달라 여기 들지 않는다.
    if follows_calendar(start, end, refs, ctx.request):
        return
    # 알림은 결제·예약만 식사의 근거다. 대화 알림만 가진 아침 체류가 `09:06 식사` 가 됐다.
    paid = {
        raw_id
        for clue in ctx.clues.notifications
        if clue.kind in (NotificationClueKind.PAYMENT, NotificationClueKind.RESERVATION)
        for raw_id in clue.raw_ids
    }
    misplaced = misplaced_clues(ctx.clues, start, end, refs)
    if not any(
        (ref.source_type is EventSourceType.PHOTO and ref.raw_id not in misplaced) or ref.raw_id in paid
        for ref in refs
    ):
        raise RepairToolError(
            "식사로 바꾸려면 이 event 가 시간이 맞는 음식 사진이나 결제 알림을 근거로 가져야 합니다. "
            "다른 묶음·다른 event 의 단서는 이 event 의 근거가 아닙니다."
        )


def _without_misplaced(
    ctx: RepairContext, client_event_id: str, fields: dict, named_photos: set[str]
) -> dict:
    """시간·장소가 맞지 않는 사진·일정 근거를 막는다(사용자 결정 2026-10-30·31).

    직접 적은 사진이 맞지 않으면 거절해 Repair 가 다시 고르게 한다. 묶음 id 로 딸려 온 것은 조용히 뺀다.
    """

    current = next((e for e in ctx.draft.events if e.client_event_id == client_event_id), None)
    if current is None:
        return fields  # 없는 event 는 update_event 가 거절한다
    tz = resolve_timezone(ctx.request.timezone)
    start = parse_datetime(fields["startTime"], tz) if fields.get("startTime") else current.start_time
    end = parse_datetime(fields["endTime"], tz) if fields.get("endTime") else current.end_time
    refs = [SourceRef.model_validate(ref) for ref in fields["sourceRefs"]]
    misplaced = misplaced_clues(ctx.clues, start or current.start_time, end or current.end_time, refs)
    named = sorted(clue for raw_id, clue in misplaced.items() if raw_id in named_photos)
    if named:
        raise RepairToolError(
            f"{', '.join(named)} 는 이 event 의 시간·장소에서 찍힌 사진이 아니라 근거가 될 수 없습니다. "
            "근거는 시간과 장소가 모두 맞는 event 에만 넣습니다."
        )
    if not misplaced:
        return fields
    kept = [ref for ref in fields["sourceRefs"] if ref.get("rawId") not in misplaced]
    if not kept:
        raise RepairToolError("evidence 가 이 event 의 시간·장소와 맞는 근거를 하나도 가리키지 않습니다.")
    return {**fields, "sourceRefs": kept}


def _named_photos(ctx: RepairContext, identifiers: list) -> set[str]:
    """`evidence` 에 Repair 가 직접 적은 사진의 rawId(단서 id `P2` 나 rawId). 묶음 id 로 딸려 온 사진은 아니다."""

    owners = _source_index(ctx.request)
    named = [str(i) for i in identifiers if ctx.clues.bundle(str(i)) is None]
    [refs] = expand_evidence(ctx.clues, [[i for i in named if i not in owners]], ctx.request)
    return {ref.raw_id for ref in refs if ref.source_type is EventSourceType.PHOTO} | {
        i for i in named if i in owners and owners[i][0] is EventSourceType.PHOTO
    }


def _with_expanded_evidence(ctx: RepairContext, fields: dict, client_event_id: str) -> dict:
    """v4 의 `evidence`(묶음·단서 id 와 rawId)를 `sourceRefs` 로 펼친다(#169).

    도구 인자의 근거를 펼치는 자리는 여기 하나다. 묶음 id 는 그 구간과 붙은 사진·일정·대화 전부이고,
    rawId 는 그대로 근거가 된다. 펼친 근거가 하나도 없으면 고치지 않는다. 다른 event 가 이미
    가진 사진·대화는 묶음을 펼칠 때 빼고 단서 id 로 직접 적은 것만 담는다 — Timeline 출력을
    펼칠 때와 같은 규칙이다.
    """

    if ctx.clues is None:
        raise RepairToolError("evidence 는 v4 세트에서만 씁니다. sourceRefs 를 주세요.")
    identifiers = fields.get("evidence")
    if not isinstance(identifiers, list) or not identifiers:
        raise RepairToolError("evidence 는 묶음·단서 id 나 rawId 의 목록이어야 합니다.")
    raw_ids = [str(i) for i in identifiers if str(i) in _source_index(ctx.request)]
    ids = [str(i) for i in identifiers if str(i) not in raw_ids]
    held = {
        ref.raw_id
        for event in ctx.draft.events
        if event.client_event_id != client_event_id
        for ref in event.source_refs
    }
    [refs] = expand_evidence(ctx.clues, [ids], ctx.request, held_elsewhere=held)
    known = {ref.raw_id for ref in refs}
    owners = _source_index(ctx.request)
    for raw_id in raw_ids:
        if raw_id not in known:
            refs.append(
                SourceRef(
                    source_type=owners[raw_id][0], raw_id=raw_id, reason="Repair 가 고른 근거"
                )
            )
            known.add(raw_id)
    if not refs:
        raise RepairToolError(f"evidence {identifiers} 가 입력의 근거를 하나도 가리키지 않습니다.")
    updated = {key: value for key, value in fields.items() if key != "evidence"}
    updated["sourceRefs"] = [ref.model_dump(by_alias=True, mode="json") for ref in refs]
    return updated


def _delete_event(ctx: RepairContext, args: dict) -> str:
    client_event_id = args.get("clientEventId")
    if not client_event_id:
        raise RepairToolError("clientEventId 인자가 필요합니다.")

    # 그 시간의 유일한 위치 근거 event 를 지우지 않는 것은 v3 세트의 계약이다(#163).
    event = delete_event(
        ctx.draft, str(client_event_id), ctx.request if ctx.extended else None
    )
    return f"{client_event_id}({event.title}) 를 지웠습니다. 남은 event={len(ctx.draft.events)}건"


def _split_event(ctx: RepairContext, args: dict) -> str:
    client_event_id = args.get("clientEventId")
    if not client_event_id:
        raise RepairToolError("clientEventId 인자가 필요합니다.")

    parts = args.get("parts")
    if not isinstance(parts, list):
        raise RepairToolError("parts 인자(조각을 둘 이상 담은 목록)가 필요합니다.")
    for part in parts:
        if isinstance(part, dict):
            _require_type_of_the_set(ctx, part.get("eventType"))

    if ctx.clues is not None:
        # 근거 없는 식사 조각을 만들지 않는다(사용자 결정 2026-10-31). 나눠 본 결과로 확인하고, 맞으면
        # 실제 draft 를 나눈다 — 근거는 코드가 원본 시각을 보고 조각마다 담는다.
        trial = ctx.draft.model_copy(deep=True)
        for piece in split_event(trial, str(client_event_id), parts, ctx.request):
            if piece.event_type is EventType.MEAL:
                _require_meal_evidence_of(ctx, piece.start_time, piece.end_time, piece.source_refs)
    pieces = split_event(ctx.draft, str(client_event_id), parts, ctx.request)
    titles = ", ".join(piece.title for piece in pieces)
    message = f"{client_event_id} 를 {len(pieces)}개로 나눴습니다: {titles}"
    if len(pieces) < len(parts):
        message += (
            f". 원래 event 의 시간 밖에 있던 조각 {len(parts) - len(pieces)}개는 뺐습니다"
        )
    return message


def _absorb_location_event(ctx: RepairContext, args: dict) -> str:
    client_event_id = args.get("clientEventId")
    into_client_event_id = args.get("intoClientEventId")
    if not client_event_id or not into_client_event_id:
        raise RepairToolError("clientEventId 와 intoClientEventId 인자가 필요합니다.")

    target = absorb_location_event(
        ctx.draft, ctx.request, str(client_event_id), str(into_client_event_id)
    )
    return (
        f"{client_event_id} 를 {into_client_event_id}({target.title}) 에 흡수했습니다. "
        f"남은 event={len(ctx.draft.events)}건"
    )


# --- 결정론 서비스 재적용 ------------------------------------------------------


def _service_tool(
    name: str,
    description: str,
    apply: Callable[[RepairContext], None],
) -> RepairTool:
    """결정론 서비스를 도구로 감싼다. 인자는 없고, 적용 후 event 수를 알려준다."""

    def run(ctx: RepairContext, args: dict) -> str:
        before = len(ctx.draft.events)
        apply(ctx)
        after = len(ctx.draft.events)
        if before == after:
            return f"{name} 을 다시 적용했습니다. events={after}건"
        return f"{name} 을 다시 적용했습니다. events={before} → {after}건"

    return RepairTool(name=name, usage=f"{name}()", description=description, run=run)


def _merge_stay_events(ctx: RepairContext) -> None:
    # 체류 병합은 event 가 시간순일 때만 옳게 묶인다.
    sort_events(ctx.draft)
    merge_stay_events(ctx.draft, ctx.request)


def _check_photo_assignment(ctx: RepairContext, args: dict) -> str:
    """사진 귀속 상태를 사람이 읽을 문장으로 돌려준다.

    draft 를 바꾸지 않는다. 어느 event 가 그 사진의 주인인지는 시각만으로 정할 수 없어
    (시각이 가깝다고 의미까지 맞지는 않는다) Repair Agent 가 `update_event` 로 정한다.
    """

    assignment = inspect_photo_assignment(ctx.draft, ctx.request)
    if not assignment.input_raw_ids:
        return "입력에 사진이 없습니다."

    missing = sorted(assignment.missing)
    duplicated = assignment.duplicated
    if not missing and not duplicated:
        return (
            f"사진 {len(assignment.input_raw_ids)}장이 모두 정확히 하나의 event 에 "
            "연결돼 있습니다."
        )

    lines = [
        f"사진 {len(assignment.input_raw_ids)}장 중 "
        f"{assignment.assigned_count}장이 정상 연결됐습니다."
    ]
    if missing:
        lines.append(f"어느 event 에도 없는 사진: {', '.join(missing)}")
    for raw_id, event_ids in sorted(duplicated.items()):
        lines.append(f"중복 연결된 사진 {raw_id}: {', '.join(event_ids)}")
    return "\n".join(lines)


# --- 상류 Agent 재실행 ---------------------------------------------------------


def _rerun_event_agent(ctx: RepairContext, args: dict) -> str:
    agent_name = args.get("agent") or args.get("name")
    if not agent_name:
        raise RepairToolError(
            f"agent 인자가 필요합니다. 사용 가능: {', '.join(sorted(ctx.event_agents))}"
        )

    agent = ctx.event_agents.get(str(agent_name))
    if agent is None:
        raise RepairToolError(
            f"'{agent_name}' Event Agent 가 없습니다. "
            f"사용 가능: {', '.join(sorted(ctx.event_agents))}"
        )

    # EventAgent.generate 는 자기 실패를 warning 으로 흡수하므로 예외로 새지 않는다.
    result = agent.generate(ctx.request)
    ctx.event_results[str(agent_name)] = result
    return (
        f"{agent_name} Event Agent 를 다시 돌렸습니다: "
        f"candidates={len(result.candidates)}, fragments={len(result.fragments)}. "
        "이 결과를 draft 에 반영하려면 rerun_timeline_agent 를 호출하세요."
    )


def _rerun_timeline_agent(ctx: RepairContext, args: dict) -> str:
    if ctx.timeline_agent is None:
        raise RepairToolError("Timeline Agent 가 연결되지 않아 재실행할 수 없습니다.")

    with execution_scope(ExecutionStage.TIMELINE_AGENT, agent="timeline"):
        if ctx.clues is not None:
            # v4 는 같은 근거 묶음으로 다시 돌린다. 코드가 만든 단서는 다시 돌려도 같다.
            draft = ctx.timeline_agent.generate_from_clues(ctx.request, ctx.clues)
        else:
            merged = merge_event_results(list(ctx.event_results.values()), ctx.request)
            draft = ctx.timeline_agent.generate(ctx.request, merged)
    # 새 draft 는 `event-001` 부터 번호를 갖고 온다. 그대로 두면 로그와 보정 기록에 남은 옛
    # 번호가 새 event 를 가리킨다(#144). 임시 id 로 바꿔 다음 확정에서 새 번호를 받게 한다.
    for index, event in enumerate(draft.events, start=1):
        event.client_event_id = f"rerun-{index:03d}"
    ctx.draft = draft
    return (
        f"Timeline Agent 를 다시 돌려 draft 를 새로 만들었습니다: "
        f"events={len(ctx.draft.events)}건. 이전 draft 의 수정 내용은 남지 않습니다."
    )


# --- 카탈로그 ------------------------------------------------------------------

_TOOLS: dict[str, RepairTool] = {
    tool.name: tool
    for tool in [
        RepairTool(
            name="lookup_source",
            usage='lookup_source(rawId="...")',
            description="근거 원본 입력 항목을 그대로 꺼내 본다. event 가 인용한 rawId 가 실제로 무엇인지 확인할 때 쓴다.",
            run=_lookup_source,
        ),
        RepairTool(
            name="update_event",
            usage='update_event(clientEventId="event-003", fields={"title": "...", "endTime": "..."})',
            description=(
                "event 한 건의 지정한 필드만 고친다. 바꿀 수 있는 필드: eventType, title, "
                "description, address, place, tags, startTime, endTime, confidence, "
                "inferenceLevel, sourceRefs, uncertainty."
            ),
            run=_update_event,
        ),
        RepairTool(
            name="delete_event",
            usage='delete_event(clientEventId="event-003")',
            description=(
                "근거가 없거나 사실이 아닌 event 를 지운다. 그 시간에 어디 있었는지를 말하는 "
                "유일한 event(체류·이동 근거를 다른 event 가 덮지 않음)는 지우지 못한다."
            ),
            run=_delete_event,
        ),
        RepairTool(
            name="split_event",
            usage=(
                'split_event(clientEventId="event-003", parts=[{"startTime": "...", '
                '"endTime": "...", "eventType": "MOVEMENT", "title": "...", '
                '"description": "..."}, {...}])'
            ),
            description=(
                "event 한 건을 둘 이상으로 나눈다. 조각마다 startTime, endTime, title 은 "
                "반드시 주고 eventType, description, place, address, confidence, "
                "inferenceLevel, uncertainty, tags 는 바꿀 때만 준다. 원래 event 의 시간 밖으로 "
                "나간 조각은 안쪽만 남기고, 안에 남는 조각이 둘보다 적으면 거절한다. 조각끼리 "
                "겹치면 안 되고, 원래 event 가 근거로 댄 체류·이동·일정마다 그 시간과 겹치는 "
                "조각이 있어야 하며, 조각마다 그 시간의 근거가 하나 이상 있어야 한다. "
                "sourceRefs 는 주지 않는다 — 원래 event 의 근거를 코드가 각 조각의 시간에 맞춰 "
                "나눠 담는다. 사진은 가장 가까운 조각에 담기므로 split 으로 사진을 떼어 낼 수 "
                "없다(사진은 update_event 로 옮긴다)."
            ),
            run=_split_event,
            extended_only=True,
        ),
        RepairTool(
            name="absorb_location_event",
            usage=(
                'absorb_location_event(clientEventId="event-002", '
                'intoClientEventId="event-003")'
            ),
            description=(
                "근거가 체류뿐인 event(clientEventId, 결제·예약이 아닌 알림이 붙어 있어도 "
                "된다)를 같은 방문을 그린 일정·사진 event(intoClientEventId)에 흡수한다. "
                "체류 근거와 그 알림을 옮기고 대상의 시간을 "
                "체류까지 넓힌 뒤 체류 event 를 지운다. 대상의 제목·본문·eventType 은 "
                "바꾸지 않는다. 흡수하면 MEAL 이 60분을 넘을 때는 그 체류 안에 식사의 사진이나 "
                "결제 알림이 있고 3시간 이하일 때(식당에 머문 식사)만 받고, 아니면 거절한다."
            ),
            run=_absorb_location_event,
            extended_only=True,
        ),
        _service_tool(
            "repair_durations",
            "지속시간이 0 인 event 를 근거 원본의 시간으로 되살리고, 순간이어야 할 event 를 시작 시각으로 되돌린다.",
            lambda ctx: repair_durations(ctx.draft, ctx.request),
        ),
        _service_tool(
            "align_location_events",
            "체류·이동 근거만 가진 event 의 시간을 그 근거 구간에 맞춘다.",
            lambda ctx: align_location_events(ctx.draft, ctx.request),
        ),
        _service_tool(
            "enforce_meal_duration",
            "MEAL event 의 지속시간을 20~60분으로 되돌린다.",
            # 확정 pass 와 같은 기준이어야 한다. v3 는 식당에 머문 식사를 3시간까지 둔다(#163).
            lambda ctx: enforce_meal_duration(
                ctx.draft,
                ctx.request,
                allow_stay_meal=ctx.extended,
                keep_calendar_meal=ctx.clues is not None,
            ),
        ),
        _service_tool(
            "enforce_sleep_boundary",
            "기상 이전 event 를 지우고, 수면 구간에 걸친 event 를 잘라 낸다.",
            lambda ctx: enforce_sleep_boundary(ctx.draft, ctx.request),
        ),
        _service_tool(
            "resolve_places",
            "place 를 근거의 장소명으로 확정하고, 근거에 없는 address 를 지운다.",
            # 확정 pass 와 같은 기준이다. v3 는 빈 place 를 채우지 않는다(#150).
            lambda ctx: resolve_places(ctx.draft, ctx.request, fill_empty=not ctx.extended),
        ),
        _service_tool(
            "ensure_calendar_events",
            "timeline 에서 통째로 빠진 캘린더 일정을 event 로 되살린다.",
            lambda ctx: ensure_calendar_events(ctx.draft, ctx.request),
        ),
        _service_tool(
            "merge_stay_events",
            "이동 없이 같은 장소에서 이어진 체류 event 들을 하나로 합친다.",
            _merge_stay_events,
        ),
        _service_tool(
            "resolve_overlaps",
            "같은 사건을 가리키는 중복 event 를 병합하고, 모순되는 겹침은 경고로 남긴다.",
            lambda ctx: resolve_overlaps(ctx.draft),
        ),
        _service_tool(
            "reinforce_calendar_location",
            "캘린더 장소와 체류 장소가 일치하면 confidence 를 올린다.",
            lambda ctx: reinforce_calendar_location(ctx.draft, ctx.request),
        ),
        RepairTool(
            name="check_photo_assignment",
            usage="check_photo_assignment()",
            description=(
                "선택한 사진이 최종 event 에 어떻게 연결됐는지 대조한다. 어느 event 에도 "
                "없는 사진과 여러 event 에 중복으로 들어간 사진을 알려 준다. 고치지는 "
                "않는다 — 어느 event 가 그 사진의 주인인지는 update_event 로 정한다."
            ),
            run=_check_photo_assignment,
        ),
        RepairTool(
            name="rerun_event_agent",
            usage='rerun_event_agent(agent="location")',
            description=(
                "특정 source 의 Event Agent 를 다시 돌려 후보를 새로 뽑는다. 그 source 의 "
                "해석이 통째로 잘못됐을 때만 쓴다. 결과를 draft 에 반영하려면 이어서 "
                "rerun_timeline_agent 를 호출해야 한다."
            ),
            run=_rerun_event_agent,
        ),
        RepairTool(
            name="rerun_timeline_agent",
            usage="rerun_timeline_agent()",
            description=(
                "Timeline Agent 를 다시 돌려 draft 를 새로 만든다. draft 의 구성 자체가 "
                "잘못됐을 때만 쓴다. 지금까지의 event 수정 내용은 사라진다."
            ),
            run=_rerun_timeline_agent,
        ),
    ]
}


#: v4 세트에서 빼는 도구(#167·#169). Event Agent 결과가 없어 다시 돌릴 Agent 가 없고,
#: 같은 장소 체류는 위치 뼈대가 이미 묶었다.
_CLUE_EXCLUDED_TOOLS = frozenset({"rerun_event_agent", "merge_stay_events"})

#: v4 세트에서 다르게 도는 도구.
_CLUE_TOOLS: dict[str, RepairTool] = {
    tool.name: tool
    for tool in [
        RepairTool(
            name="update_event",
            usage=(
                'update_event(clientEventId="event-003", fields={"title": "...", '
                '"evidence": ["B3", "P2"]})'
            ),
            description=(
                "event 한 건의 지정한 필드만 고친다. 바꿀 수 있는 필드: eventType, title, "
                "description, startTime, endTime, confidence, inferenceLevel, uncertainty, "
                "placeCandidates, evidence. evidence 는 근거 묶음·단서 id(B3·P2·C1·N5)나 "
                "rawId 의 목록이고 event 의 근거를 통째로 바꾼다. 사진은 한 event 에만 있으므로 "
                "단서 id 로 직접 적은 사진은 이 event 로 옮겨지고 다른 event 에서 빠진다. 묶음 id 는 "
                "다른 event 가 가진 사진·대화를 가져오지 않는다. 장소(place·address)는 "
                "코드가 근거로 정하므로 고치지 않는다."
            ),
            run=_update_event,
        ),
        _service_tool(
            "resolve_places",
            "장소 칸을 근거로 다시 확정한다. 근거가 이름을 가리키면 확정하고, 아니면 "
            "placeCandidates 를 후보 풀 안의 것만 남긴다.",
            lambda ctx: confirm_place_slots(ctx.draft, ctx.request, ctx.clues),
        ),
    ]
}


def available_tools(ctx: RepairContext) -> dict[str, RepairTool]:
    """이 실행에서 쓸 수 있는 도구. 카탈로그에 싣는 것과 실행을 허용하는 것이 같아야 한다."""

    tools = {
        name: tool
        for name, tool in _TOOLS.items()
        if ctx.extended or not tool.extended_only
    }
    if ctx.clues is None:
        return tools
    return {
        name: _CLUE_TOOLS.get(name, tool)
        for name, tool in tools.items()
        if name not in _CLUE_EXCLUDED_TOOLS
    }


def tool_catalog_text(ctx: RepairContext) -> str:
    """도구 카탈로그를 프롬프트용 텍스트로 만든다."""

    lines: list[str] = []
    for tool in available_tools(ctx).values():
        lines.append(f"- `{tool.usage}`: {tool.description}")
    if ctx.event_agents:
        lines.append(
            f"  (rerun_event_agent 로 다시 돌릴 수 있는 agent: "
            f"{', '.join(sorted(ctx.event_agents))})"
        )
    return "\n".join(lines)


def execute_tool_calls(
    ctx: RepairContext, tool_calls: list[RepairToolCall]
) -> list[RepairToolResult]:
    """계획의 도구 호출을 순서대로 실행하고 결과를 돌려준다.

    한 도구가 실패해도 멈추지 않는다. 실패는 `ok=False` 결과로 남아 다음 분석
    프롬프트에 실리고, LLM 이 그것을 보고 다시 판단한다.
    """

    results: list[RepairToolResult] = []
    tools = available_tools(ctx)
    for call in tool_calls:
        started = perf_counter()
        trace_input = {
            "call": call.model_dump(by_alias=True, mode="json"),
            "timeline": ctx.draft.model_dump(by_alias=True, mode="json"),
        }
        tool = tools.get(call.tool)
        if tool is None:
            with trace_observation(
                f"execute-{call.tool.replace('_', '-')}",
                as_type="span",
                input=trace_input,
                metadata={"tool": call.tool},
            ) as observation:
                result = RepairToolResult(
                    tool=call.tool,
                    ok=False,
                    message=f"없는 도구입니다. 사용 가능: {', '.join(sorted(tools))}",
                )
                update_observation(
                    observation,
                    output={
                        "ok": False,
                        "errorCode": int(ErrorCode.REPAIR_TOOL_FAILED),
                        "durationMs": (perf_counter() - started) * 1000,
                        "result": result.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                    level="ERROR",
                    status_message="등록되지 않은 Repair 도구입니다.",
                )
                results.append(result)
            continue

        # 도구와 상류 재실행은 Repair Agent의 내부 실행 상세다. Agent Graph에는
        # 고정된 Main/Event/Timeline/Repair 역할만 보이도록 span으로 기록한다.
        with (
            token_usage_scope() as token_usage,
            trace_observation(
                f"execute-{call.tool.replace('_', '-')}",
                as_type="span",
                input=trace_input,
                metadata={"tool": call.tool},
            ) as observation,
        ):
            try:
                message = tool.run(ctx, call.args)
                result = RepairToolResult(
                    tool=call.tool, ok=True, message=message
                )
                update_observation(
                    observation,
                    output={
                        "ok": True,
                        "durationMs": (perf_counter() - started) * 1000,
                        "tokenUsage": token_usage.summary(),
                        "result": result.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                )
                results.append(result)
            except Exception as exc:  # noqa: BLE001 - 한 도구 실패는 격리한다.
                failure_code = code_of(exc)
                report_error(
                    logger,
                    failure_code,
                    "Repair 도구 실행 실패",
                    exc=exc,
                    # 인자 **값**은 남기지 않는다. LLM 이 만든 편집 인자에는 event
                    # title 같은 사용자 콘텐츠가 들어간다(그쪽 추적은 Langfuse 담당).
                    context={"tool": call.tool, "argumentNames": sorted(call.args)},
                    # 도구 호출마다 부른다. 실패는 Agent 에게 되돌아가 다음 반복에서
                    # 다시 시도되므로 한 작업에서 수십 건이 된다. Repair 가 통째로
                    # 실패하면 그쪽이 운영 이벤트를 낸다.
                    emit=False,
                )
                # message 는 Repair Agent에게 되돌려 주는 값이라 원문을 유지한다.
                result = RepairToolResult(
                    tool=call.tool, ok=False, message=str(exc)
                )
                update_observation(
                    observation,
                    output={
                        "ok": False,
                        "errorCode": int(failure_code),
                        "durationMs": (perf_counter() - started) * 1000,
                        "tokenUsage": token_usage.summary(),
                        "result": result.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                    level="ERROR",
                    status_message="Repair 도구 실행에 실패했습니다.",
                )
                results.append(result)

    ctx.log.extend(results)
    return results


def tool_log_text(ctx: RepairContext) -> str:
    """지금까지의 도구 실행 로그를 프롬프트용 텍스트로 만든다."""

    if not ctx.log:
        return "아직 실행한 도구가 없습니다."
    return "\n".join(
        f"- {result.tool}: {'성공' if result.ok else '실패'} — {result.message}"
        for result in ctx.log
    )


# --- 확정 pass 가 넘기는 것 (#119) ------------------------------------------------


def confirm_report_text(ctx: RepairContext) -> str:
    """코드가 고친 것과 찾은 것을 프롬프트용 텍스트로 만든다.

    찾은 것은 마지막 확정의 것만, 고친 것은 몇 번째 확정에서 나온 것인지 붙여 쌓아서
    싣는다(`reports_to_prompt`).
    """

    payload = reports_to_prompt(ctx.reports)
    if not payload:
        return "코드가 고치거나 찾은 것이 없습니다."
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _candidate_summary(agent_name: str, candidate) -> dict:
    time_range = candidate.time_range
    return {
        "agent": agent_name,
        "eventType": candidate.event_type.value,
        "title": candidate.title,
        "startTime": time_range.start_time.isoformat(),
        "endTime": time_range.end_time.isoformat(),
    }


def clue_evidence_text(ctx: RepairContext) -> str:
    """v4 의 `[event 근거]`(#169): 근거 묶음 전부와, event 마다 쓴 묶음·단서 id 와 병합 이유.

    묶음 본문은 Timeline v4 가 본 것과 같은 모양으로 한 번만 싣는다. event 는 id 로 가리킨다.
    어느 event 도 쓰지 않은 단서는 `unusedClues` 다 — Timeline 이 일부러 쓰지 않았을 수 있고,
    그 이유는 warning 에 있다.
    """

    clues = ctx.clues
    if clues is None:
        return "없음"
    sections = timeline_input_sections(clues)
    owners = _clue_owners(clues)
    used: set[str] = set()
    events = []
    for event in ctx.draft.events:
        ids: list[str] = []
        for ref in event.source_refs:
            for identifier in owners.get(ref.raw_id, ()):
                if identifier not in ids:
                    ids.append(identifier)
        used.update(ids)
        entry: dict[str, object] = {"clientEventId": event.client_event_id, "evidence": ids}
        if event.base_event_ids:
            entry["from"] = list(event.base_event_ids)
        if event.merge_reason:
            entry["mergeReason"] = event.merge_reason
        events.append(entry)

    every = [bundle.bundle_id for bundle in clues.bundles]
    every.extend(photo.clue_id for photo in clues.photos)
    every.extend(calendar.clue_id for calendar in clues.calendars)
    every.extend(clue.clue_id for clue in clues.notifications)
    payload = {
        "day": sections["day"],
        "bundles": sections["bundles"],
        "emptyTimes": sections["emptyTimes"],
        "notifications": sections["notifications"],
        "events": events,
        "unusedClues": [identifier for identifier in every if identifier not in used],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _clue_owners(clues: TimelineClues) -> dict[str, list[str]]:
    """rawId → 그 rawId 를 담은 묶음·단서 id."""

    owners: dict[str, list[str]] = {}

    def add(raw_id: str, identifier: str) -> None:
        owners.setdefault(raw_id, [])
        if identifier not in owners[raw_id]:
            owners[raw_id].append(identifier)

    for bundle in clues.bundles:
        if bundle.segment is not None:
            for raw_id in bundle.segment.raw_ids:
                add(raw_id, bundle.bundle_id)
        for record in bundle.isolated:
            add(record.raw_id, bundle.bundle_id)
    for photo in clues.photos:
        add(photo.raw_id, photo.clue_id)
    for calendar in clues.calendars:
        add(calendar.raw_id, calendar.clue_id)
    for clue in clues.notifications:
        for raw_id in clue.raw_ids:
            add(raw_id, clue.clue_id)
    return owners


def event_evidence_text(ctx: RepairContext) -> str:
    """event 마다 그 event 가 참조한 rawId 의 candidate·fragment 를 모은다.

    `[근거 원본]` 은 raw 입력의 한 줄 요약이라 사진에 무엇이 찍혔는지, 알림이 무슨
    내용인지가 없다. 그것은 Event Agent 가 해석해 candidate 의 `description` 에 적었고
    Timeline 은 그것을 보고 문장을 썼다. Repair 가 같은 수준으로 다시 쓰려면 같은 근거를
    봐야 한다.

    candidate 본문은 한 번만 싣고 event 는 id 로 가리킨다. 한 candidate 를 여러 event 가
    근거로 삼아도 본문이 되풀이되지 않는다. 어느 event 에도 쓰이지 않은 candidate 는 한 줄
    요약만 싣는다 — Timeline 이 일부러 쓰지 않았을 수 있고, 그 이유는 warning 에 있다.
    """

    candidates: dict[str, dict] = {}
    unused: list[dict] = []
    events: dict[str, dict] = {
        event.client_event_id: {"candidateIds": [], "fragments": []}
        for event in ctx.draft.events
    }
    event_raw_ids = {
        event.client_event_id: {ref.raw_id for ref in event.source_refs}
        for event in ctx.draft.events
    }

    for agent_name, result in ctx.event_results.items():
        for candidate in result.candidates:
            raw_ids = {ref.raw_id for ref in candidate.source_refs}
            users = [
                event_id
                for event_id, used in event_raw_ids.items()
                if used & raw_ids
            ]
            summary = _candidate_summary(agent_name, candidate)
            if not users:
                unused.append(summary)
                continue

            candidate_id = f"candidate-{len(candidates) + 1:03d}"
            detail = {**summary, "description": candidate.description}
            # 장소 후보(#140). event 의 `place` 는 이 목록에서 고른 것이라, 다른 후보로 바꿀지
            # 판단하려면 목록을 봐야 한다. `[근거 원본]` 은 단수 `place` 만 보인다.
            if candidate.places:
                detail["places"] = list(candidate.places)
            if candidate.address:
                detail["address"] = candidate.address
            if candidate.uncertainty:
                detail["uncertainty"] = list(candidate.uncertainty)
            candidates[candidate_id] = detail
            for event_id in users:
                events[event_id]["candidateIds"].append(candidate_id)

        for fragment in result.fragments:
            for event_id, used in event_raw_ids.items():
                if fragment.raw_id in used:
                    events[event_id]["fragments"].append(
                        {"agent": agent_name, "summary": fragment.summary}
                    )

    if not candidates and not unused and not any(
        entry["fragments"] for entry in events.values()
    ):
        return "없음"

    payload = {
        "candidates": candidates,
        "events": [
            {"clientEventId": event_id, **entry}
            for event_id, entry in events.items()
            if entry["candidateIds"] or entry["fragments"]
        ],
        "unusedCandidates": unused,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
