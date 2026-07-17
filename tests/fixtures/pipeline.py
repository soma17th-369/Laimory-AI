"""Timeline Agent → 결정론 확정까지 실제 main agent 그래프로 통과시키는 헬퍼.

`TimelineAgent.generate()` 는 LLM 병합과 파싱까지만 한다. 정렬·겹침·지속시간·
sourceRef·clientEventId 같은 확정은 Repair Agent 안의 `draft_repair` 가 맡는다.
따라서 "LLM 이 이런 draft 를 돌려주면 최종 결과는 이렇게 된다" 를 검증하는 테스트는
그래프를 그대로 태워야 한다. 테스트가 배선을 흉내 내면 배선이 깨져도 테스트는 통과한다.

여기서는 Repair Agent 의 **반복 상한을 0 으로** 준다. 즉 LLM 개선 없이 결정론 확정만
지나간다. 이 헬퍼를 쓰는 테스트들이 검증하는 것이 그 확정 로직이기 때문이다. Repair
Agent 의 LLM 분석·도구 호출은 `tests/agents/test_repair_agent.py` 가 따로 검증한다.
"""

import asyncio

from app.agents.events.base_event_agent import EventAgent
from app.agents.main import run_main_agent
from app.agents.repair import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.schemas import AgentEventResult, TimelineDraft, TimelineDraftRequest
from tests.fixtures.fake_llm import FakeLLM


class StubEventAgent(EventAgent):
    """미리 만든 `AgentEventResult` 를 그대로 돌려주는 Event Agent."""

    def __init__(self, result: AgentEventResult, name: str = "stub") -> None:
        self.name = name
        self._result = result

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        return self._result


def confirm_only_repair_agent() -> RepairAgent:
    """LLM 을 부르지 않고 결정론 확정만 하는 Repair Agent."""

    return RepairAgent(max_iterations=0)


def run_timeline_pipeline(
    request: TimelineDraftRequest,
    agent_result: AgentEventResult,
    llm_response: str,
) -> TimelineDraft:
    """FakeLLM 의 draft 응답을 Timeline Agent → 결정론 확정으로 통과시킨다."""

    return asyncio.run(
        run_main_agent(
            request,
            event_agents=[StubEventAgent(agent_result)],
            timeline_agent=TimelineAgent(llm=FakeLLM([llm_response])),
            repair_agent=confirm_only_repair_agent(),
        )
    )
