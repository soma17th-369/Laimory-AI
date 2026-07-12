"""병렬 메인 에이전트 검증.

Event Agent 병렬 팬아웃 → 취합 → Timeline Agent 로 draft 를 만드는 흐름과,
개별 Event Agent 실패가 전체를 멈추지 않고 warning 으로 이어지는지 확인한다.
"""

import asyncio
import json

from app.agents.events.base_event_agent import EventAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.agents.main import run_main_agent
from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
)
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import make_request, stay_item


def _request():
    """후보가 참조하는 rawId 를 실제로 담은 요청(sourceRef 검증을 통과하도록)."""

    return make_request(
        stays=[stay_item(1, raw_id="s-1"), stay_item(2, raw_id="s-2")]
    )


def _candidate(source_id: str) -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.REST,
        time_range=CandidateTimeRange(
            start_time="2026-06-20T09:00:00+09:00",
            end_time="2026-06-20T10:00:00+09:00",
        ),
        title="체류",
        description="설명",
        source_refs=[SourceRef(source_type=EventSourceType.STAY, source_id=source_id)],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        uncertainty=["x"],
    )


class _StubAgent(EventAgent):
    def __init__(self, name: str, result: AgentEventResult) -> None:
        self.name = name
        self._result = result

    def _generate(self, request) -> AgentEventResult:
        return self._result


class _BoomAgent(EventAgent):
    name = "boom"

    def _generate(self, request) -> AgentEventResult:
        raise RuntimeError("의도된 실패")


def _timeline_agent_returning_one_event() -> TimelineAgent:
    draft_json = json.dumps(
        {
            "userId": "u",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                {
                    "eventType": "REST",
                    "title": "체류",
                    "description": "",
                    "startTime": "2026-06-20T09:00:00+09:00",
                    "endTime": "2026-06-20T10:00:00+09:00",
                    "confidence": 0.8,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [{"sourceType": "STAY", "sourceId": "s-1"}],
                }
            ],
        },
        ensure_ascii=False,
    )
    return TimelineAgent(llm=FakeLLM([draft_json]))


def _timeline_agent_returning_unsorted_events() -> TimelineAgent:
    """시간 순서가 뒤집힌 draft 를 돌려주는 Timeline Agent."""

    def event(title, start, end):
        return {
            "eventType": "REST",
            "title": title,
            "description": "",
            "startTime": f"2026-06-20T{start}:00+09:00",
            "endTime": f"2026-06-20T{end}:00+09:00",
            "confidence": 0.8,
            "inferenceLevel": "EVIDENCE_BASED",
            "sourceRefs": [{"sourceType": "STAY", "rawId": "s-1"}],
        }

    draft_json = json.dumps(
        {
            "userId": "u",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [event("오후", "15:00", "16:00"), event("오전", "09:00", "10:00")],
        },
        ensure_ascii=False,
    )
    return TimelineAgent(llm=FakeLLM([draft_json]))


def test_repair_node_sorts_events_and_reassigns_ids():
    # Timeline Agent 는 뒤집힌 순서를 돌려주지만, repair node 가 확정한다.
    draft = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=[_StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")]))],
            timeline_agent=_timeline_agent_returning_unsorted_events(),
        )
    )

    assert [event.title for event in draft.events] == ["오전", "오후"]
    assert [event.client_event_id for event in draft.events] == ["event-001", "event-002"]


def test_main_agent_merges_and_builds_draft():
    agents = [
        _StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")])),
        _StubAgent("b", AgentEventResult(candidates=[_candidate("s-2")])),
    ]

    draft = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=agents,
            timeline_agent=_timeline_agent_returning_one_event(),
        )
    )

    assert len(draft.events) == 1
    assert draft.events[0].client_event_id == "event-001"


def test_main_agent_isolates_failing_event_agent():
    agents = [
        _StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")])),
        _BoomAgent(),
    ]

    draft = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=agents,
            timeline_agent=_timeline_agent_returning_one_event(),
        )
    )

    # 실패 agent 는 전체를 멈추지 않고, 성공 agent 의 이벤트가 draft 로 이어진다.
    assert len(draft.events) == 1
    # 실패는 upstream warning 으로 draft 에 남는다.
    assert any("boom" in w.message for w in draft.warnings)

