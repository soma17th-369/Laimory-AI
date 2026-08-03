"""Repair Agent 도구 카탈로그.

Repair Agent 는 draft 를 직접 다시 쓰지 않는다. **이미 있는 결정론 서비스와 Agent 를
도구로 호출**해서 고친다. 도구는 세 종류다.

    1. 조회   : `lookup_source` 로 근거 원본을 되짚는다.
    2. 편집   : `update_event` / `delete_event` 로 event 를 고치거나 지운다.
    3. 재적용 : `enforce_sleep_boundary`, `resolve_places` 같은 결정론 서비스를 다시 돌린다.
    4. 재실행 : `rerun_event_agent` / `rerun_timeline_agent` 로 상류 Agent 를 다시 돌린다.

3번이 도구인 이유: 이 서비스들은 `repair_draft` 가 이미 한 번 돌린 것들이다. 하지만
Repair Agent 가 event 를 고치거나 상류 Agent 를 다시 돌리면 그 확정이 무너지므로,
LLM 이 "무엇을 다시 확정해야 하는지" 골라 부를 수 있어야 한다. 로직을 복제하지 않고
같은 함수를 그대로 부른다.

**정렬·`clientEventId` 재부여·window 강제는 도구가 아니다.** 그 셋은 결과가 반드시
일관돼야 하는 처리라 LLM 의 선택지로 두지 않는다. Repair Agent 는 매 반복이 끝날 때
`repair_draft` 를 통째로 다시 돌리고, 그 안에서 코드가 항상 확정한다.

도구 실행 실패는 예외로 새지 않고 `RepairToolResult(ok=False)` 로 돌아가 다음 분석
프롬프트에 실린다. LLM 이 자기가 뭘 잘못 불렀는지 보고 고쳐 부를 수 있게 하기 위해서다.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from time import perf_counter

from app.agents.events import merge_event_results
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
    HealthMetric,
    RepairToolCall,
    RepairToolResult,
    TimelineDraft,
    TimelineDraftRequest,
)
from app.services.calendar_guard import ensure_calendar_events
from app.services.calendar_location import reinforce_calendar_location
from app.services.draft_edit import delete_event, update_event
from app.services.draft_repair import (
    align_location_events,
    merge_stay_events,
    repair_durations,
    resolve_overlaps,
    sort_events,
)
from app.services.meal_guard import enforce_meal_duration
from app.services.photo_guard import inspect_photo_assignment
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError, code_of, report_error
from app.services.place_resolver import resolve_places
from app.services.sleep_guard import enforce_sleep_boundary
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)


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


@dataclass(frozen=True)
class RepairTool:
    """도구 하나. `usage` 는 그대로 프롬프트의 도구 카탈로그에 실린다."""

    name: str
    usage: str
    description: str
    run: Callable[[RepairContext, dict], str]


# --- 조회 --------------------------------------------------------------------

#: HEALTH 는 하나의 itemType 이지만 metric 으로 SLEEP/ACTIVITY 를 나눈다.
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


def source_index_text(request: TimelineDraftRequest) -> str:
    """근거 원본 목록을 한 줄씩 요약한다(프롬프트용).

    전체 항목을 통째로 싣지 않는 이유는 프롬프트가 비대해지기 때문이다. 자세한 값이
    필요하면 LLM 이 `lookup_source` 로 그 항목만 꺼내 본다.
    """

    lines: list[str] = []
    for raw_id, (source_type, item) in _source_index(request).items():
        start = getattr(item, "start_at", None) or getattr(item, "taken_at", None) or getattr(item, "posted_at", None) or ""
        end = getattr(item, "end_at", None) or ""
        span = f"{start}~{end}" if end else str(start)
        label = _item_label(item)
        lines.append(f"- {source_type.value} rawId={raw_id} {span} {label}".rstrip())
    return "\n".join(lines) if lines else "없음"


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
    payload = item.model_dump(by_alias=True, mode="json")
    return f"{source_type.value} {json.dumps(payload, ensure_ascii=False)}"


# --- 편집 --------------------------------------------------------------------


def _update_event(ctx: RepairContext, args: dict) -> str:
    client_event_id = args.get("clientEventId")
    if not client_event_id:
        raise RepairToolError("clientEventId 인자가 필요합니다.")

    fields = args.get("fields")
    if not isinstance(fields, dict):
        raise RepairToolError("fields 인자(바꿀 필드만 담은 객체)가 필요합니다.")

    event = update_event(ctx.draft, str(client_event_id), fields)
    return f"{client_event_id} 를 수정했습니다: {event.title} ({', '.join(sorted(fields))})"


def _delete_event(ctx: RepairContext, args: dict) -> str:
    client_event_id = args.get("clientEventId")
    if not client_event_id:
        raise RepairToolError("clientEventId 인자가 필요합니다.")

    event = delete_event(ctx.draft, str(client_event_id))
    return f"{client_event_id}({event.title}) 를 지웠습니다. 남은 event={len(ctx.draft.events)}건"


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

    merged = merge_event_results(list(ctx.event_results.values()))
    with execution_scope(ExecutionStage.TIMELINE_AGENT, agent="timeline"):
        ctx.draft = ctx.timeline_agent.generate(ctx.request, merged)
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
                "description, address, placeLabel, tags, startTime, endTime, confidence, "
                "inferenceLevel, sourceRefs, uncertainty."
            ),
            run=_update_event,
        ),
        RepairTool(
            name="delete_event",
            usage='delete_event(clientEventId="event-003")',
            description="근거가 없거나 사실이 아닌 event 를 지운다.",
            run=_delete_event,
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
            lambda ctx: enforce_meal_duration(ctx.draft, ctx.request),
        ),
        _service_tool(
            "enforce_sleep_boundary",
            "기상 이전 event 를 지우고, 수면 구간에 걸친 event 를 잘라 낸다.",
            lambda ctx: enforce_sleep_boundary(ctx.draft, ctx.request),
        ),
        _service_tool(
            "resolve_places",
            "placeLabel 을 근거의 장소명으로 확정하고, 근거에 없는 address 를 지운다.",
            lambda ctx: resolve_places(ctx.draft, ctx.request),
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


def tool_catalog_text(ctx: RepairContext) -> str:
    """도구 카탈로그를 프롬프트용 텍스트로 만든다."""

    lines: list[str] = []
    for tool in _TOOLS.values():
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
    for call in tool_calls:
        started = perf_counter()
        trace_input = {
            "call": call.model_dump(by_alias=True, mode="json"),
            "timeline": ctx.draft.model_dump(by_alias=True, mode="json"),
        }
        tool = _TOOLS.get(call.tool)
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
                    message=f"없는 도구입니다. 사용 가능: {', '.join(sorted(_TOOLS))}",
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
