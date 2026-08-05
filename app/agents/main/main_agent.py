"""메인 에이전트.

`POST /v1/timeline` 요청 하나를 다음 순서로 조율한다.

    1. `run_event_agents`: 데이터별 Event Agent 에게 요청을 병렬 분배한다.
    2. `merge_results`: 각 Agent 의 반환(`AgentEventResult`)을 하나로 취합한다.
    3. `run_timeline_agent`: Timeline Agent 가 취합 결과로 `TimelineDraft` 를 만든다.
    4. `run_repair_agent`: Repair Agent 가 draft 를 확정하고 개선해 최종 draft 를 만든다.
    5. `run_question_agent`: 확정된 event 에 사용자용 회고 질문을 붙인다(이슈 #66).

3번까지는 LLM 이 의미를 판단하는 확률적 단계다. 4번은 그 둘이 섞여 있다. Repair Agent
안에서 **코드가 draft 를 확정하고(`draft_repair`), LLM 이 남은 문제를 도구로 고친다.**
정렬이나 `clientEventId` 처럼 반드시 맞아야 하는 것은 그 안에서도 여전히 코드가 한다
(`app/agents/repair/` 참고).

5번이 Repair 뒤인 것은 순서 취향이 아니다. Repair 가 event 를 병합·삭제하고
`clientEventId` 를 다시 매기므로, 그전에 만든 질문은 사라진 event 를 가리키게 된다.

Event Agent 결과는 **Agent 이름을 키로** 들고 다닌다. Repair Agent 가 특정 Event Agent
만 다시 돌린 뒤 Timeline Agent 를 재실행할 때, 다시 돌린 Agent 의 결과만 갈아 끼우고
나머지는 처음 결과를 그대로 써야 하기 때문이다. 이름을 짓는 책임은 여기에 있다.

각 Event Agent 의 실행 실패는 이미 `EventAgent.generate()` 가 warning 으로
흡수하고, Timeline Agent 의 실패도 빈 draft + warning 으로 흡수하며, Repair Agent 의
개선 실패도 직전 draft 를 유지한다. Question Agent 의 실패는 이 모듈이 흡수한다 —
질문은 타임라인에 얹는 부가 가치라 없다고 해서 하루 기록을 버릴 이유가 없다. 그래서
이 메인 에이전트는 개별 Agent 실패로 중단되지 않는다. 전체 흐름은 LangGraph 로 구성해 단계별 상태를 명시하고, timeout 은
호출자가 `run_main_agent` 을 `asyncio.wait_for` 로 감싸 처리한다.
"""

import asyncio
from time import perf_counter
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.events import default_event_agents, merge_event_results
from app.agents.events.base_event_agent import EventAgent
from app.agents.question.question_agent import QuestionAgent
from app.agents.repair.repair_agent import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import code_of_or, report_error
from app.core.langfuse_tracing import (
    token_usage_scope,
    trace_observation,
    update_observation,
)
from app.core.execution_context import ExecutionStage
from app.core.logging import get_logger, log_fields, stage_span
from app.schemas import (
    AgentEventResult,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)

logger = get_logger(__name__)


class _MainAgentState(TypedDict, total=False):
    request: TimelineDraftRequest
    event_agents: dict[str, EventAgent]
    timeline_agent: TimelineAgent
    repair_agent: RepairAgent
    question_agent: QuestionAgent
    event_results: dict[str, AgentEventResult]
    merged_result: AgentEventResult
    draft: TimelineDraft


def _name_agents(agents: list[EventAgent]) -> dict[str, EventAgent]:
    """Event Agent 에 유일한 이름을 붙인다.

    이름은 Repair Agent 가 "어떤 Agent 를 다시 돌릴지" 고르는 열쇠이자 결과를 갈아
    끼우는 키다. 같은 이름이 둘이면 한 쪽 결과가 조용히 사라지므로 번호를 붙여 나눈다.
    """

    named: dict[str, EventAgent] = {}
    for agent in agents:
        base = agent.name or type(agent).__name__
        name = base
        suffix = 2
        while name in named:
            name = f"{base}-{suffix}"
            suffix += 1
        named[name] = agent
    return named


def _request_trace_input(request: TimelineDraftRequest) -> dict[str, object]:
    """Agent가 실제로 판단에 사용한 정규화 요청 전체와 요약을 반환한다."""

    return {
        "taskId": request.task_id,
        "date": request.date,
        "inputItemCounts": request.source_item_counts(),
        "hasUserMemory": request.user_memory is not None,
        "request": request.model_dump(by_alias=True, mode="json"),
    }


def _run_event_agent_traced(
    name: str,
    agent: EventAgent,
    request: TimelineDraftRequest,
) -> AgentEventResult:
    """Event Agent 실행을 구체적인 agent 관측으로 감싼다."""

    started = perf_counter()
    with (
        token_usage_scope() as token_usage,
        trace_observation(
            f"event-agent-{name.replace('_', '-')}",
            as_type="agent",
            input=_request_trace_input(request),
            metadata={"agent": name, "sourceStage": "event"},
        ) as observation,
    ):
        result = agent.generate(request)
        update_observation(
            observation,
            output={
                "candidateCount": len(result.candidates),
                "fragmentCount": len(result.fragments),
                "warningCount": len(result.warnings),
                "durationMs": (perf_counter() - started) * 1000,
                "tokenUsage": token_usage.summary(),
                "result": result.model_dump(by_alias=True, mode="json"),
            },
            level="WARNING" if result.warnings else "DEFAULT",
        )
        return result


def _build_graph():
    """타임라인 초안 생성 LangGraph 를 구성한다."""

    async def run_event_agents_node(state: _MainAgentState) -> _MainAgentState:
        request = state["request"]
        agents = state["event_agents"]

        # Agent.generate 는 내부에서 실패를 warning 으로 흡수하므로 예외로 새지 않는다.
        results = await asyncio.gather(
            *(
                asyncio.to_thread(_run_event_agent_traced, name, agent, request)
                for name, agent in agents.items()
            )
        )
        return {"event_results": dict(zip(agents, results))}

    def merge_results_node(state: _MainAgentState) -> _MainAgentState:
        started = perf_counter()
        event_results = {
            name: result.model_dump(by_alias=True, mode="json")
            for name, result in state["event_results"].items()
        }
        with trace_observation(
            "merge-event-results",
            # 병렬 Event Agent가 Timeline Agent로 합류하는 명시적 fan-in이다.
            # Agent Graph의 시간 기반 추론이 Event Agent들을 서로 연결하지 않도록
            # 구조 노드로 남긴다.
            as_type="chain",
            input={"eventResults": event_results},
            metadata={"agentCount": len(event_results)},
        ) as observation:
            merged = merge_event_results(list(state["event_results"].values()))
            update_observation(
                observation,
                output={
                    "durationMs": (perf_counter() - started) * 1000,
                    "mergedResult": merged.model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                },
                level="WARNING" if merged.warnings else "DEFAULT",
            )
        logger.debug(
            "이벤트 후보 취합 완료",
            extra=log_fields(
                candidateCount=len(merged.candidates),
                fragmentCount=len(merged.fragments),
                warningCount=len(merged.warnings),
                durationMs=round((perf_counter() - started) * 1000, 3),
            ),
        )
        return {"merged_result": merged}

    async def run_timeline_agent_node(state: _MainAgentState) -> _MainAgentState:
        with (
            stage_span(
                logger,
                ExecutionStage.TIMELINE_AGENT,
                agent="timeline",
            ) as outcome,
            token_usage_scope() as token_usage,
            trace_observation(
                "timeline-agent",
                as_type="agent",
                input={
                    "request": state["request"].model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                    "mergedResult": state["merged_result"].model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                },
                metadata={"agent": "timeline"},
            ) as langfuse_observation,
        ):
            started = perf_counter()
            # 실패는 흡수하지도, 여기서 기록하지도 않고 그대로 올린다. 다시 던지는
            # 경계마다 남기면 실패 하나가 여러 건으로 집계된다 — 코드 부여와 최종
            # 기록은 timeline_runner 가 소유한다(이슈 #53).
            draft = await asyncio.to_thread(
                state["timeline_agent"].generate,
                state["request"],
                state["merged_result"],
            )
            update_observation(
                langfuse_observation,
                output={
                    "eventCount": len(draft.events),
                    "questionCount": len(draft.questions),
                    "warningCount": len(draft.warnings),
                    "durationMs": (perf_counter() - started) * 1000,
                    "tokenUsage": token_usage.summary(),
                    "timeline": draft.model_dump(by_alias=True, mode="json"),
                },
                level="WARNING" if draft.warnings else "DEFAULT",
            )
            outcome["eventCount"] = len(draft.events)
            outcome["questionCount"] = len(draft.questions)
            outcome["warningCount"] = len(draft.warnings)
        return {"draft": draft}

    async def run_repair_agent_node(state: _MainAgentState) -> _MainAgentState:
        # Repair Agent 는 LLM 을 여러 번 부르는 블로킹 호출이라 스레드에 올린다.
        with (
            stage_span(
                logger,
                ExecutionStage.REPAIR_AGENT,
                agent="repair",
                maxIterations=settings.repair_max_iterations,
            ) as outcome,
            token_usage_scope() as token_usage,
            trace_observation(
                "repair-agent",
                as_type="agent",
                input={
                    "request": state["request"].model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                    "eventResults": {
                        name: result.model_dump(by_alias=True, mode="json")
                        for name, result in state["event_results"].items()
                    },
                    "timeline": state["draft"].model_dump(
                        by_alias=True,
                        mode="json",
                    ),
                },
                metadata={
                    "agent": "repair",
                    "maxIterations": settings.repair_max_iterations,
                },
            ) as langfuse_observation,
        ):
            started = perf_counter()
            # Timeline Agent 노드와 같은 이유로 흡수하지도, 기록하지도 않는다.
            draft = await asyncio.to_thread(
                lambda: state["repair_agent"].generate(
                    state["request"],
                    state["draft"],
                    event_results=state["event_results"],
                    event_agents=state["event_agents"],
                    timeline_agent=state["timeline_agent"],
                )
            )
            update_observation(
                langfuse_observation,
                output={
                    "eventCount": len(draft.events),
                    "questionCount": len(draft.questions),
                    "warningCount": len(draft.warnings),
                    "durationMs": (perf_counter() - started) * 1000,
                    "tokenUsage": token_usage.summary(),
                    "timeline": draft.model_dump(by_alias=True, mode="json"),
                },
                level="WARNING" if draft.warnings else "DEFAULT",
            )
            outcome["eventCount"] = len(draft.events)
            outcome["questionCount"] = len(draft.questions)
            outcome["warningCount"] = len(draft.warnings)
        return {"draft": draft}

    async def run_question_agent_node(state: _MainAgentState) -> _MainAgentState:
        # 질문 생성은 실패해도 타임라인을 버리지 않는다. 다른 Agent 와 달리 흡수를
        # 여기서 하는 이유는, Question Agent 자신은 "질문 없는 draft" 라는 대체
        # 산출물을 갖고 있지 않아서다 — 원본 draft 는 호출부인 여기에 있다.
        draft = state["draft"]
        with (
            stage_span(
                logger,
                ExecutionStage.QUESTION_AGENT,
                agent="question",
            ) as outcome,
            token_usage_scope() as token_usage,
            trace_observation(
                "question-agent",
                as_type="agent",
                input={
                    "date": state["request"].date,
                    "timeline": draft.model_dump(by_alias=True, mode="json"),
                },
                metadata={"agent": "question"},
            ) as langfuse_observation,
        ):
            started = perf_counter()
            try:
                draft = await asyncio.to_thread(
                    state["question_agent"].generate,
                    state["request"],
                    draft,
                )
            except Exception as exc:  # noqa: BLE001 - 질문 실패는 흡수한다
                failure_code = code_of_or(exc, ErrorCode.QUESTION_GENERATION_FAILED)
                report_error(
                    logger,
                    failure_code,
                    "Question Agent 실행 실패",
                    exc=exc,
                    context={"fallback": "draft_without_questions"},
                    stage=ExecutionStage.QUESTION_AGENT,
                    exc_info=True,
                )
                draft.warnings.append(
                    TimelineWarning(
                        warning_id="warning-question-001",
                        severity=TimelineWarningSeverity.LOW,
                        message="기록 질문을 만들지 못해 질문 없이 진행했습니다.",
                    )
                )
                update_observation(
                    langfuse_observation,
                    output={
                        "errorCode": int(failure_code),
                        "durationMs": (perf_counter() - started) * 1000,
                        "tokenUsage": token_usage.summary(),
                    },
                    level="ERROR",
                    status_message="기록 질문 생성에 실패했습니다.",
                )
                outcome["errorCode"] = int(failure_code)
                return {"draft": draft}

            update_observation(
                langfuse_observation,
                output={
                    "eventCount": len(draft.events),
                    "durationMs": (perf_counter() - started) * 1000,
                    "tokenUsage": token_usage.summary(),
                    "timeline": draft.model_dump(by_alias=True, mode="json"),
                },
            )
            outcome["eventCount"] = len(draft.events)
        return {"draft": draft}

    graph = StateGraph(_MainAgentState)
    graph.add_node("run_event_agents", run_event_agents_node)
    graph.add_node("merge_results", merge_results_node)
    graph.add_node("run_timeline_agent", run_timeline_agent_node)
    graph.add_node("run_repair_agent", run_repair_agent_node)
    graph.add_node("run_question_agent", run_question_agent_node)
    graph.add_edge(START, "run_event_agents")
    graph.add_edge("run_event_agents", "merge_results")
    graph.add_edge("merge_results", "run_timeline_agent")
    graph.add_edge("run_timeline_agent", "run_repair_agent")
    graph.add_edge("run_repair_agent", "run_question_agent")
    graph.add_edge("run_question_agent", END)
    return graph.compile()


async def run_main_agent(
    request: TimelineDraftRequest,
    *,
    event_agents: list[EventAgent] | None = None,
    timeline_agent: TimelineAgent | None = None,
    repair_agent: RepairAgent | None = None,
    question_agent: QuestionAgent | None = None,
) -> TimelineDraft:
    """요청을 받아 확정된 `TimelineDraft` 를 생성한다.

    Args:
        request: 하루 라이프로그 입력 요청.
        event_agents: 사용할 Event Agent 목록. 기본값은 `default_event_agents()`.
        timeline_agent: 사용할 Timeline Agent. 기본값은 새 `TimelineAgent()`.
        repair_agent: 사용할 Repair Agent. 기본값은 새 `RepairAgent()`.
        question_agent: 사용할 Question Agent. 기본값은 새 `QuestionAgent()`.

    Event Agent 들은 각각 블로킹 LLM 호출을 하므로 `asyncio.to_thread` 로
    스레드에 올려 동시에 실행한다. Timeline/Repair Agent 역시 블로킹이라 스레드에서
    실행한다.
    """

    agents = _name_agents(
        event_agents if event_agents is not None else default_event_agents()
    )
    timeline = timeline_agent if timeline_agent is not None else TimelineAgent()
    repair = repair_agent if repair_agent is not None else RepairAgent()
    question = question_agent if question_agent is not None else QuestionAgent()

    # 이 타임라인을 "어떤 provider/model 로 만들었는지" 를 로그에 남긴다. 모든 하위
    # 에이전트는 설정된 provider(`settings.llm_provider`)와 그 provider 의 모델을 쓰므로
    # 실행에 사용될 모델은 설정에서 결정론적으로 정해진다.
    provider = settings.llm_provider
    model = getattr(settings, f"{provider}_model", "")

    with (
        stage_span(
            logger,
            ExecutionStage.MAIN_AGENT,
            agent="main",
            agentCount=len(agents),
            provider=provider,
            model=model,
        ) as outcome,
        token_usage_scope() as token_usage,
        trace_observation(
            "main-agent",
            as_type="agent",
            input=_request_trace_input(request),
            metadata={
                "agentCount": len(agents),
                "provider": provider,
                "model": model,
            },
        ) as langfuse_observation,
    ):
        started = perf_counter()
        final_state = await _build_graph().ainvoke(
            {
                "request": request,
                "event_agents": agents,
                "timeline_agent": timeline,
                "repair_agent": repair,
                "question_agent": question,
            }
        )
        draft = final_state["draft"]
        update_observation(
            langfuse_observation,
            output={
                "eventCount": len(draft.events),
                "questionCount": len(draft.questions),
                "warningCount": len(draft.warnings),
                "durationMs": (perf_counter() - started) * 1000,
                "tokenUsage": token_usage.summary(),
                "timeline": draft.model_dump(by_alias=True, mode="json"),
            },
            level="WARNING" if draft.warnings else "DEFAULT",
        )
        outcome["eventCount"] = len(draft.events)
        outcome["questionCount"] = len(draft.questions)
        outcome["warningCount"] = len(draft.warnings)
    return draft
