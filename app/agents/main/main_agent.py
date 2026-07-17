"""메인 에이전트.

`POST /v1/timeline` 요청 하나를 다음 순서로 조율한다.

    1. `run_event_agents`: 데이터별 Event Agent 에게 요청을 병렬 분배한다.
    2. `merge_results`: 각 Agent 의 반환(`AgentEventResult`)을 하나로 취합한다.
    3. `run_timeline_agent`: Timeline Agent 가 취합 결과로 `TimelineDraft` 를 만든다.
    4. `run_repair_agent`: Repair Agent 가 draft 를 확정하고 개선해 최종 draft 를 만든다.

3번까지는 LLM 이 의미를 판단하는 확률적 단계다. 4번은 그 둘이 섞여 있다. Repair Agent
안에서 **코드가 draft 를 확정하고(`draft_repair`), LLM 이 남은 문제를 도구로 고친다.**
정렬이나 `clientEventId` 처럼 반드시 맞아야 하는 것은 그 안에서도 여전히 코드가 한다
(`app/agents/repair/` 참고).

Event Agent 결과는 **Agent 이름을 키로** 들고 다닌다. Repair Agent 가 특정 Event Agent
만 다시 돌린 뒤 Timeline Agent 를 재실행할 때, 다시 돌린 Agent 의 결과만 갈아 끼우고
나머지는 처음 결과를 그대로 써야 하기 때문이다. 이름을 짓는 책임은 여기에 있다.

각 Event Agent 의 실행 실패는 이미 `EventAgent.generate()` 가 warning 으로
흡수하고, Timeline Agent 의 실패도 빈 draft + warning 으로 흡수하며, Repair Agent 의
개선 실패도 직전 draft 를 유지한다. 그래서 이 메인 에이전트는 개별 Agent 실패로
중단되지 않는다. 전체 흐름은 LangGraph 로 구성해 단계별 상태를 명시하고, timeout 은
호출자가 `run_main_agent` 을 `asyncio.wait_for` 로 감싸 처리한다.
"""

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.events import default_event_agents, merge_event_results
from app.agents.events.base_event_agent import EventAgent
from app.agents.repair.repair_agent import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.logging import get_logger
from app.schemas import AgentEventResult, TimelineDraft, TimelineDraftRequest

logger = get_logger(__name__)


class _MainAgentState(TypedDict, total=False):
    request: TimelineDraftRequest
    event_agents: dict[str, EventAgent]
    timeline_agent: TimelineAgent
    repair_agent: RepairAgent
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


def _build_graph():
    """타임라인 초안 생성 LangGraph 를 구성한다."""

    async def run_event_agents_node(state: _MainAgentState) -> _MainAgentState:
        request = state["request"]
        agents = state["event_agents"]

        # Agent.generate 는 내부에서 실패를 warning 으로 흡수하므로 예외로 새지 않는다.
        results = await asyncio.gather(
            *(asyncio.to_thread(agent.generate, request) for agent in agents.values())
        )
        return {"event_results": dict(zip(agents, results))}

    def merge_results_node(state: _MainAgentState) -> _MainAgentState:
        merged = merge_event_results(list(state["event_results"].values()))
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

    async def run_repair_agent_node(state: _MainAgentState) -> _MainAgentState:
        # Repair Agent 는 LLM 을 여러 번 부르는 블로킹 호출이라 스레드에 올린다.
        draft = await asyncio.to_thread(
            lambda: state["repair_agent"].generate(
                state["request"],
                state["draft"],
                event_results=state["event_results"],
                event_agents=state["event_agents"],
                timeline_agent=state["timeline_agent"],
            )
        )
        logger.info(
            "타임라인 초안 확정 완료: events=%d, questions=%d, warnings=%d",
            len(draft.events),
            len(draft.questions),
            len(draft.warnings),
        )
        return {"draft": draft}

    graph = StateGraph(_MainAgentState)
    graph.add_node("run_event_agents", run_event_agents_node)
    graph.add_node("merge_results", merge_results_node)
    graph.add_node("run_timeline_agent", run_timeline_agent_node)
    graph.add_node("run_repair_agent", run_repair_agent_node)
    graph.add_edge(START, "run_event_agents")
    graph.add_edge("run_event_agents", "merge_results")
    graph.add_edge("merge_results", "run_timeline_agent")
    graph.add_edge("run_timeline_agent", "run_repair_agent")
    graph.add_edge("run_repair_agent", END)
    return graph.compile()


async def run_main_agent(
    request: TimelineDraftRequest,
    *,
    event_agents: list[EventAgent] | None = None,
    timeline_agent: TimelineAgent | None = None,
    repair_agent: RepairAgent | None = None,
) -> TimelineDraft:
    """요청을 받아 확정된 `TimelineDraft` 를 생성한다.

    Args:
        request: 하루 라이프로그 입력 요청.
        event_agents: 사용할 Event Agent 목록. 기본값은 `default_event_agents()`.
        timeline_agent: 사용할 Timeline Agent. 기본값은 새 `TimelineAgent()`.
        repair_agent: 사용할 Repair Agent. 기본값은 새 `RepairAgent()`.

    Event Agent 들은 각각 블로킹 LLM 호출을 하므로 `asyncio.to_thread` 로
    스레드에 올려 동시에 실행한다. Timeline/Repair Agent 역시 블로킹이라 스레드에서
    실행한다.
    """

    agents = _name_agents(
        event_agents if event_agents is not None else default_event_agents()
    )
    timeline = timeline_agent if timeline_agent is not None else TimelineAgent()
    repair = repair_agent if repair_agent is not None else RepairAgent()

    logger.info(
        "메인 에이전트 시작: taskId=%s, agents=%d",
        request.task_id,
        len(agents),
    )

    final_state = await _build_graph().ainvoke(
        {
            "request": request,
            "event_agents": agents,
            "timeline_agent": timeline,
            "repair_agent": repair,
        }
    )
    return final_state["draft"]
