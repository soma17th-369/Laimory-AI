"""메인 에이전트.

`POST /v1/timeline` 요청 하나를 다음 순서로 조율한다.

    1. `run_event_agents`: 데이터별 Event Agent 에게 요청을 병렬 분배한다.
    2. `merge_results`: 각 Agent 의 반환(`AgentEventResult`)을 하나로 취합한다.
    3. `run_timeline_agent`: Timeline Agent 가 취합 결과로 `TimelineDraft` 를 만든다.
    4. `repair_draft`: LLM draft 를 코드로 확정한다(정렬·겹침·지속시간·sourceRef·id).

3번까지는 LLM 이 의미를 판단하는 확률적 단계이고, 4번은 결정론적 단계다. 이벤트 순서나
`clientEventId` 처럼 반드시 맞아야 하는 것들을 LLM 에 맡기지 않으려고 별도 node 로
분리했다. 실제 구현은 `app/services/draft_repair.py` 에 있다.

각 Event Agent 의 실행 실패는 이미 `EventAgent.generate()` 가 warning 으로
흡수하고, Timeline Agent 의 실패도 빈 draft + warning 으로 흡수하므로, 이
메인 에이전트는 개별 Agent 실패로 중단되지 않는다. 전체 흐름은 LangGraph 로 구성해
단계별 상태를 명시하고, timeout 은 호출자가 `run_main_agent` 을
`asyncio.wait_for` 로 감싸 처리한다.
"""

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.events import default_event_agents
from app.agents.events.base_event_agent import EventAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.logging import get_logger
from app.schemas import AgentEventResult, TimelineDraft, TimelineDraftRequest
from app.services.draft_repair import repair_draft

logger = get_logger(__name__)


class _MainAgentState(TypedDict, total=False):
    request: TimelineDraftRequest
    event_agents: list[EventAgent]
    timeline_agent: TimelineAgent
    event_results: list[AgentEventResult]
    merged_result: AgentEventResult
    draft: TimelineDraft


def _merge_results(results: list[AgentEventResult]) -> AgentEventResult:
    """Event Agent 결과들을 하나의 `AgentEventResult` 로 취합한다."""

    merged = AgentEventResult()
    for result in results:
        merged.candidates.extend(result.candidates)
        merged.fragments.extend(result.fragments)
        merged.warnings.extend(result.warnings)
    return merged


def _build_graph():
    """타임라인 초안 생성 LangGraph 를 구성한다."""

    async def run_event_agents_node(state: _MainAgentState) -> _MainAgentState:
        request = state["request"]
        agents = state["event_agents"]

        # Agent.generate 는 내부에서 실패를 warning 으로 흡수하므로 예외로 새지 않는다.
        results = await asyncio.gather(
            *(asyncio.to_thread(agent.generate, request) for agent in agents)
        )
        return {"event_results": results}

    def merge_results_node(state: _MainAgentState) -> _MainAgentState:
        merged = _merge_results(state["event_results"])
        logger.info(
            "이벤트 후보 취합 완료: candidates=%d, fragments=%d, warnings=%d",
            len(merged.candidates),
            len(merged.fragments),
            len(merged.warnings),
        )
        return {"merged_result": merged}

    async def run_timeline_agent_node(state: _MainAgentState) -> _MainAgentState:
        draft = await asyncio.to_thread(
            state["timeline_agent"].generate,
            state["request"],
            state["merged_result"],
        )
        logger.info(
            "타임라인 초안 생성 완료: events=%d, questions=%d, warnings=%d",
            len(draft.events),
            len(draft.questions),
            len(draft.warnings),
        )
        return {"draft": draft}

    def repair_draft_node(state: _MainAgentState) -> _MainAgentState:
        # LLM 이 만든 draft 를 코드로 확정한다. 순수 계산이라 스레드로 넘기지 않는다.
        return {"draft": repair_draft(state["draft"], state["request"])}

    graph = StateGraph(_MainAgentState)
    graph.add_node("run_event_agents", run_event_agents_node)
    graph.add_node("merge_results", merge_results_node)
    graph.add_node("run_timeline_agent", run_timeline_agent_node)
    graph.add_node("repair_draft", repair_draft_node)
    graph.add_edge(START, "run_event_agents")
    graph.add_edge("run_event_agents", "merge_results")
    graph.add_edge("merge_results", "run_timeline_agent")
    graph.add_edge("run_timeline_agent", "repair_draft")
    graph.add_edge("repair_draft", END)
    return graph.compile()


async def run_main_agent(
    request: TimelineDraftRequest,
    *,
    event_agents: list[EventAgent] | None = None,
    timeline_agent: TimelineAgent | None = None,
) -> TimelineDraft:
    """요청을 받아 `TimelineDraft` 를 생성한다.

    Args:
        request: 하루 라이프로그 입력 요청.
        event_agents: 사용할 Event Agent 목록. 기본값은 `default_event_agents()`.
        timeline_agent: 사용할 Timeline Agent. 기본값은 새 `TimelineAgent()`.

    Event Agent 들은 각각 블로킹 LLM 호출을 하므로 `asyncio.to_thread` 로
    스레드에 올려 동시에 실행한다. Timeline Agent 역시 블로킹이라 스레드에서
    실행한다.
    """

    agents = event_agents if event_agents is not None else default_event_agents()
    agent = timeline_agent if timeline_agent is not None else TimelineAgent()

    logger.info(
        "메인 에이전트 시작: taskId=%s, agents=%d",
        request.task_id,
        len(agents),
    )

    final_state = await _build_graph().ainvoke(
        {
            "request": request,
            "event_agents": agents,
            "timeline_agent": agent,
        }
    )
    return final_state["draft"]
