"""Repair Agent.

Timeline Agent 가 만든 draft 를 **검토하고 개선해 최종 draft 를 확정한다.** 기존의
결정론 repair 단계(`app/services/draft_repair.py`)를 없애지 않고 이 Agent 안으로
들여왔다. 한 번의 실행은 이렇게 흐른다.

    1. `repair_draft`         : 코드가 draft 를 확정한다(정렬·겹침·수면·window·id).
    2. `analyze`              : LLM 이 확정된 draft 를 근거 원본과 대조해 문제를 찾고
                                도구 호출 계획(`RepairPlan`)을 낸다.
    3. `execute`              : 계획의 도구를 순서대로 실행한다(수정·삭제·서비스 재적용·
                                Event/Timeline Agent 재실행).
    4. `repair_draft`         : 고친 draft 를 코드가 다시 확정한다.
    5. 2~4 를 `done` 이거나 반복 상한(`settings.repair_max_iterations`)까지 되풀이한다.

**LLM 은 확정된 draft 만 본다.** 매 반복이 코드 확정으로 끝나므로, 다음 분석은 항상
정렬·id 가 맞은 상태에서 시작한다. 반대로 정렬·`clientEventId` 재부여·window 강제는
도구로 노출하지 않는다(`tools.py` 참고). 결과가 반드시 일관돼야 하는 처리를 LLM 의
선택에 맡기면, LLM 이 그 도구를 부르지 않는 순간 깨지기 때문이다.

실패는 draft 를 잃지 않는다. LLM 호출·응답 파싱이 실패하면 **마지막으로 확정된
draft**(첫 확정 또는 마지막 성공 반복의 결과)를 돌려주고 warning 을 남긴다. 개별 도구
호출의 실패는 애초에 예외로 새지 않고 실패 결과로 남아 다음 분석 프롬프트에 실린다.
"""

import json
from collections.abc import Callable
from time import perf_counter
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.base import Agent
from app.agents.events.base_event_agent import EventAgent
from app.agents.parsing import SupportsComplete, default_llm
from app.agents.prompt_loader import load_prompt
from app.agents.repair.tools import (
    RepairContext,
    execute_tool_calls,
    source_index_text,
    tool_catalog_text,
    tool_log_text,
)
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.config import settings
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import code_of_or, report_error
from app.core.langfuse_tracing import trace_observation, update_observation
from app.core.execution_context import ExecutionStage, execution_scope
from app.core.logging import get_logger, log_fields
from app.core.structured import StructuredOutputError
from app.schemas import (
    AgentEventResult,
    RepairPlan,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.draft_repair import repair_draft
from app.services.fragment_guard import verify_fragment_usage

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")


def parse_repair_plan(text: str) -> RepairPlan:
    """LLM 응답을 `RepairPlan` 으로 파싱한다.

    코드펜스나 앞뒤 설명이 붙어 나와도 첫 ``{`` 부터 마지막 ``}`` 까지를 잘라 읽는다.
    파싱이나 스키마 검증에 실패하면 예외를 던져 상위 fallback 으로 넘긴다. 계획을
    반쯤 읽어 반쯤 적용하는 것보다, 이번 개선을 포기하고 확정된 draft 를 지키는 편이 낫다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("Repair Agent 응답에서 JSON 객체를 찾지 못했습니다.")

    payload = json.loads(text[start : end + 1])
    return RepairPlan.model_validate(payload)


def build_repair_prompt(ctx: RepairContext, remaining: int) -> str:
    """분석 요청(user prompt): 확정된 draft + 근거 원본 + 도구 + 지금까지의 실행 로그."""

    draft_text = json.dumps(
        ctx.draft.model_dump(by_alias=True, mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return (
        f"[draft]\n{draft_text}\n\n"
        f"[근거 원본]\n{source_index_text(ctx.request)}\n\n"
        f"[사용 가능한 도구]\n{tool_catalog_text(ctx)}\n\n"
        f"[지금까지 실행한 도구]\n{tool_log_text(ctx)}\n\n"
        f"[남은 반복 횟수] 이번 차례를 포함해 {remaining}번\n\n"
        "이 draft 의 문제를 근거 원본과 대조해 찾고, 고칠 도구 호출 계획을 JSON 으로 내세요. "
        "고칠 것이 없으면 done 을 true 로 하고 toolCalls 를 비웁니다."
    )


def _dedupe_warnings(draft: TimelineDraft) -> None:
    """같은 내용의 warning 을 하나만 남긴다(in-place).

    `repair_draft` 는 반복마다 다시 돌아가므로 같은 확정 경고("중복 event 를 병합
    했습니다" 등)를 매번 새로 붙인다. 사용자에게 같은 말을 세 번 보여 줄 이유는 없다.
    """

    seen: set[tuple[str, str]] = set()
    kept: list[TimelineWarning] = []
    for warning in draft.warnings:
        key = (warning.severity.value, warning.message)
        if key in seen:
            continue
        seen.add(key)
        kept.append(warning)
    draft.warnings = kept


def _fragment_raw_ids(ctx: RepairContext) -> set[str]:
    """Event Agent 들이 fragment 로 남긴 rawId 전체."""

    return {
        str(fragment.raw_id)
        for result in ctx.event_results.values()
        for fragment in result.fragments
    }


def _confirm(ctx: RepairContext) -> None:
    """코드 확정 패스. 매 반복의 끝이자 다음 분석의 시작 상태를 만든다.

    확정이 끝나면 `ctx.on_confirm` 으로 **복사본**을 발행한다(이슈 #76). 발행 지점이
    여기 하나뿐인 것은 의도다 — 정렬·`clientEventId` 가 맞은 draft 만 저장 대상이 될 수
    있어야 한다. 복사본인 것도 의도다. 호출자가 제한 시간으로 이 실행을 취소해도
    `asyncio.to_thread` 위의 스레드는 계속 돌며 `ctx.draft` 를 마저 고치므로, 참조를
    넘기면 이미 발행한 값이 뒤늦게 바뀐다.
    """

    repair_draft(ctx.draft, ctx.request)
    # 확정된 event 를 대상으로 본다. 병합·삭제로 구성이 바뀐 뒤라야 "이 event 의 근거가
    # 정말 fragment 뿐인가" 를 옳게 판정한다.
    verify_fragment_usage(ctx.draft, _fragment_raw_ids(ctx))
    _dedupe_warnings(ctx.draft)
    if ctx.on_confirm is not None:
        ctx.on_confirm(ctx.draft.model_copy(deep=True))


class _State(TypedDict, total=False):
    iteration: int
    plan: RepairPlan


class RepairAgent(Agent):
    """draft 의 문제를 분석하고 도구로 고쳐 최종 draft 를 확정하는 Agent."""

    name = "repair"

    def __init__(
        self,
        llm: SupportsComplete | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self._llm = llm
        self._max_iterations = (
            max_iterations
            if max_iterations is not None
            else settings.repair_max_iterations
        )

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def generate(
        self,
        request: TimelineDraftRequest,
        draft: TimelineDraft,
        *,
        event_results: dict[str, AgentEventResult] | None = None,
        event_agents: dict[str, EventAgent] | None = None,
        timeline_agent: TimelineAgent | None = None,
        on_confirm: Callable[[TimelineDraft], None] | None = None,
    ) -> TimelineDraft:
        """확정·개선을 마친 최종 `TimelineDraft` 를 돌려준다.

        Args:
            request: 하루 라이프로그 입력 요청.
            draft: Timeline Agent 가 만든(아직 확정되지 않은) draft.
            event_results: Agent 이름 → 그 Agent 의 결과. `rerun_timeline_agent` 가
                다시 병합할 때 쓴다.
            event_agents: Agent 이름 → Event Agent. `rerun_event_agent` 가 다시 돌린다.
            timeline_agent: `rerun_timeline_agent` 로 다시 돌릴 Timeline Agent.
            on_confirm: 확정된 draft 복사본을 받을 콜백(이슈 #76). 매 확정마다 불린다.
                호출자가 제한 시간으로 이 실행을 취소해도 마지막 확정본을 저장할 수 있게
                하는 통로다.

        `event_results` 와 `event_agents` 는 **같은 이름 키**를 써야 한다. 이름이
        어긋나면 다시 돌린 Agent 의 결과가 이전 결과를 대체하지 못하고 나란히 쌓여,
        같은 후보가 두 벌 병합된다. 이름을 짓는 쪽은 호출자(main agent)다.
        """

        ctx = RepairContext(
            request=request,
            draft=draft,
            event_results=dict(event_results or {}),
            event_agents=dict(event_agents or {}),
            timeline_agent=timeline_agent,
            on_confirm=on_confirm,
        )

        # LLM 이 무엇을 하든, 코드 확정은 반드시 한 번은 지나간다.
        started = perf_counter()
        with trace_observation(
            "confirm-timeline-draft",
            as_type="span",
            input={
                "request": request.model_dump(by_alias=True, mode="json"),
                "timeline": draft.model_dump(by_alias=True, mode="json"),
            },
            metadata={"phase": "initial"},
        ) as observation:
            _confirm(ctx)
            update_observation(
                observation,
                output={
                    "durationMs": (perf_counter() - started) * 1000,
                    "timeline": ctx.draft.model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                },
            )

        if self._max_iterations <= 0:
            logger.debug("Repair Agent 반복 상한이 0 이라 코드 확정만 수행했습니다.")
            return ctx.draft

        # 마지막으로 확정에 성공한 draft. 개선이 실패하면 이것을 돌려준다.
        last_good: list[TimelineDraft] = [ctx.draft.model_copy(deep=True)]

        try:
            self._run_graph(ctx, last_good)
        except Exception as exc:  # noqa: BLE001 - 개선 실패가 draft 를 잃게 하지 않는다
            failure_code = code_of_or(exc, ErrorCode.REPAIR_AGENT_FAILED)
            report_error(
                logger,
                failure_code,
                "Repair Agent 실행 실패",
                exc=exc,
                context={"fallback": "last_good_draft"},
                exc_info=True,
            )
            restored = last_good[0]
            restored.warnings.append(
                TimelineWarning(
                    warning_id=f"warning-repair-agent-{len(restored.warnings) + 1:03d}",
                    severity=TimelineWarningSeverity.MEDIUM,
                    message=(
                        f"{self.name} agent 개선 실패로 직전 draft 를 유지했습니다: "
                        f"{message_for(failure_code)}"
                    ),
                )
            )
            return restored

        logger.debug(
            "Repair Agent 완료: events=%d, tools=%d",
            len(ctx.draft.events),
            len(ctx.log),
        )
        return ctx.draft

    def _run_graph(self, ctx: RepairContext, last_good: list[TimelineDraft]) -> None:
        """analyze → execute → confirm 를 `done` 이나 반복 상한까지 되풀이한다.

        `last_good` 은 마지막으로 확정에 성공한 draft 를 담는 1칸짜리 목록이다. 반복
        도중에 실패하면 호출자가 이것을 돌려준다(그때까지의 개선은 남는다).
        """

        max_iterations = self._max_iterations

        def analyze_node(state: _State) -> _State:
            iteration = state["iteration"] + 1
            remaining = max_iterations - iteration + 1
            prompt = build_repair_prompt(ctx, remaining)
            started = perf_counter()
            with (
                execution_scope(
                    ExecutionStage.REPAIR_AGENT,
                    agent=self.name,
                    iteration=iteration,
                ),
                trace_observation(
                    "analyze-repair-iteration",
                    # 같은 이름의 analyze/execute/confirm이 반복되면 Langfuse
                    # Aggregated graph가 Repair loop를 cycle로 표현한다.
                    as_type="chain",
                    input={
                        "iteration": iteration,
                        "remainingIterations": remaining,
                        "system": _SYSTEM_PROMPT,
                        "prompt": prompt,
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                    metadata={
                        "iteration": iteration,
                        "maxIterations": max_iterations,
                    },
                ) as observation,
            ):
                plan = self.llm.complete_structured(
                    prompt,
                    RepairPlan,
                    system=_SYSTEM_PROMPT,
                    temperature=0.0,
                )
                update_observation(
                    observation,
                    output={
                        "durationMs": (perf_counter() - started) * 1000,
                        "plan": plan.model_dump(by_alias=True, mode="json"),
                    },
                    level="DEFAULT" if plan.done else "WARNING",
                )
            logger.debug(
                "Repair 분석 완료",
                extra=log_fields(
                    iteration=iteration,
                    maxIterations=max_iterations,
                    remainingIterations=remaining,
                    issueCount=len(plan.issues),
                    toolCallCount=len(plan.tool_calls),
                    toolNames=[call.tool for call in plan.tool_calls],
                    done=plan.done,
                    durationMs=round((perf_counter() - started) * 1000, 3),
                ),
            )
            return {"iteration": iteration, "plan": plan}

        def execute_node(state: _State) -> _State:
            iteration = state["iteration"]
            started = perf_counter()
            with (
                execution_scope(
                    ExecutionStage.REPAIR_AGENT,
                    agent=self.name,
                    iteration=iteration,
                ),
                trace_observation(
                    "execute-repair-plan",
                    as_type="chain",
                    input={
                        "iteration": iteration,
                        "plan": state["plan"].model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                    metadata={"iteration": iteration},
                ) as observation,
            ):
                results = execute_tool_calls(ctx, state["plan"].tool_calls)
                logger.debug(
                    "Repair 도구 실행 완료",
                    extra=log_fields(
                        iteration=iteration,
                        toolNames=[result.tool for result in results],
                        okCount=sum(1 for result in results if result.ok),
                        failedCount=sum(1 for result in results if not result.ok),
                    ),
                )
                update_observation(
                    observation,
                    output={
                        "durationMs": (perf_counter() - started) * 1000,
                        "results": [
                            result.model_dump(by_alias=True, mode="json")
                            for result in results
                        ],
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                    level=(
                        "WARNING"
                        if any(not result.ok for result in results)
                        else "DEFAULT"
                    ),
                )

            # execute span 밖에서 sibling으로 열어 Aggregated graph가
            # analyze → execute → confirm → analyze 순환을 추론할 수 있게 한다.
            confirm_started = perf_counter()
            with trace_observation(
                "confirm-repair-iteration",
                as_type="chain",
                input={
                    "iteration": iteration,
                    "timeline": ctx.draft.model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                },
                metadata={"iteration": iteration},
            ) as confirmation:
                _confirm(ctx)
                update_observation(
                    confirmation,
                    output={
                        "durationMs": (
                            perf_counter() - confirm_started
                        )
                        * 1000,
                        "timeline": ctx.draft.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                )
            last_good[0] = ctx.draft.model_copy(deep=True)
            logger.debug(
                "Repair 반복 draft 확정",
                extra=log_fields(
                    iteration=iteration,
                    eventCount=len(ctx.draft.events),
                    questionCount=len(ctx.draft.questions),
                    warningCount=len(ctx.draft.warnings),
                ),
            )
            return {}

        def after_analyze(state: _State) -> str:
            # 계획에 도구가 없으면(문제 없음 또는 done) 더 할 일이 없다.
            return "execute" if state["plan"].tool_calls else END

        def after_execute(state: _State) -> str:
            if state["plan"].done or state["iteration"] >= max_iterations:
                return END
            return "analyze"

        graph = StateGraph(_State)
        graph.add_node("analyze", analyze_node)
        graph.add_node("execute", execute_node)
        graph.add_edge(START, "analyze")
        graph.add_conditional_edges("analyze", after_analyze, {"execute": "execute", END: END})
        graph.add_conditional_edges("execute", after_execute, {"analyze": "analyze", END: END})

        # 반복마다 analyze + execute 두 node 를 지나므로 여유를 두고 상한을 잡는다.
        graph.compile().invoke(
            {"iteration": 0},
            {"recursion_limit": 2 * max_iterations + 5},
        )
