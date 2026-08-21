"""병렬 메인 에이전트 검증.

Event Agent 병렬 팬아웃 → 취합 → Timeline Agent → Repair Agent 로 draft 를 만드는
흐름과, 개별 Event Agent 실패가 전체를 멈추지 않고 warning 으로 이어지는지 확인한다.

Repair Agent 는 반복 상한 0(결정론 확정만) 으로 주입한다. 여기서 보려는 것은 배선과
확정 결과이고, LLM 분석·도구 호출은 `tests/agents/test_repair_agent.py` 가 본다.
"""

import asyncio
import json

from app.agents.events.base_event_agent import EventAgent
from app.agents.question import QuestionAgent
from app.agents.repair import RepairAgent
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
from tests.fixtures.pipeline import confirm_only_repair_agent, silent_question_agent
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


def _request():
    """후보가 참조하는 rawId 를 실제로 담은 요청(sourceRef 검증을 통과하도록)."""

    return make_request(
        stays=[stay_item(1, raw_id="s-1"), stay_item(2, raw_id="s-2")]
    )


def _candidate(raw_id: str) -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.REST,
        time_range=CandidateTimeRange(
            start_time="2026-06-20T09:00:00+09:00",
            end_time="2026-06-20T10:00:00+09:00",
        ),
        title="체류",
        description="설명",
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id(raw_id),
            )
        ],
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
                    "sourceRefs": [
                        {
                            "sourceType": "STAY",
                            "rawId": fixture_raw_id("s-1"),
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    return TimelineAgent(llm=FakeLLM([draft_json]))


def _timeline_agent_returning_unsorted_events() -> TimelineAgent:
    """시간 순서가 뒤집힌 draft 를 돌려주는 Timeline Agent."""

    def event(title, start, end, raw_id):
        return {
            "eventType": "REST",
            "title": title,
            "description": "",
            "startTime": f"2026-06-20T{start}:00+09:00",
            "endTime": f"2026-06-20T{end}:00+09:00",
            "confidence": 0.8,
            "inferenceLevel": "EVIDENCE_BASED",
            "sourceRefs": [
                {
                    "sourceType": "STAY",
                    "rawId": fixture_raw_id(raw_id),
                }
            ],
        }

    draft_json = json.dumps(
        {
            "userId": "u",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                event("오후", "15:00", "16:00", "s-1"),
                event("오전", "09:00", "10:00", "s-2"),
            ],
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
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
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
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
        )
    )

    assert len(draft.events) == 1
    assert draft.events[0].client_event_id == "event-001"


def test_repair_agent_receives_draft_sources_and_reruns():
    """활성 Repair Agent 가 그래프에 연결되어 draft 를 실제로 고치는지 본다.

    배선이 끊기면(예: draft 나 event_results 를 넘기지 않으면) 여기서 걸린다.
    """

    repair_plan = json.dumps(
        {
            "issues": [{"clientEventId": "event-001", "problem": "제목이 데이터 라벨"}],
            "toolCalls": [
                {
                    "tool": "update_event",
                    "args": {"clientEventId": "event-001", "fields": {"title": "카페에서 쉬었다"}},
                }
            ],
            "done": True,
        },
        ensure_ascii=False,
    )
    repair_llm = FakeLLM([repair_plan])

    draft = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=[_StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")]))],
            timeline_agent=_timeline_agent_returning_one_event(),
            repair_agent=RepairAgent(llm=repair_llm, max_iterations=2),
            question_agent=silent_question_agent(),
        )
    )

    assert [event.title for event in draft.events] == ["카페에서 쉬었다"]

    # Timeline Agent 가 만든 draft 와, 다시 돌릴 수 있는 Event Agent 이름이 실려야 한다.
    prompt = repair_llm.calls[0].prompt
    assert "[draft]" in prompt
    assert f"rawId={fixture_raw_id('s-1')}" in prompt
    assert "rerun_event_agent" in prompt and "a" in prompt


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
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
        )
    )

    # 실패 agent 는 전체를 멈추지 않고, 성공 agent 의 이벤트가 draft 로 이어진다.
    assert len(draft.events) == 1
    # 실패는 upstream warning 으로 draft 에 남는다.
    assert any("boom" in w.message for w in draft.warnings)
    assert all("의도된 실패" not in w.message for w in draft.warnings)


def test_question_node_attaches_questions_after_repair():
    """질문은 Repair 가 id 를 다시 매긴 **뒤에** 붙는다(이슈 #66).

    Timeline Agent 가 뒤집힌 순서를 돌려주므로 repair 전후의 `clientEventId` 가
    다르다. 질문이 repair 이후의 id 를 기준으로 붙는지 여기서 갈린다.
    """

    question_json = json.dumps(
        {
            "questions": [
                {"clientEventId": "event-001", "question": "오전은 어떻게 보내셨나요?"}
            ]
        },
        ensure_ascii=False,
    )

    draft = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=[_StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")]))],
            timeline_agent=_timeline_agent_returning_unsorted_events(),
            repair_agent=confirm_only_repair_agent(),
            question_agent=QuestionAgent(llm=FakeLLM([question_json])),
        )
    )

    # repair 가 시간 순으로 정렬하고 id 를 다시 매긴 뒤의 event-001 은 "오전" 이다.
    assert draft.events[0].title == "오전"
    assert draft.events[0].question == "오전은 어떻게 보내셨나요?"
    assert draft.events[1].question is None


def test_question_failure_keeps_the_timeline():
    """질문 생성이 깨져도 타임라인은 그대로 남고 warning 만 붙는다."""

    class _BoomQuestionAgent(QuestionAgent):
        def generate(self, request, draft):
            raise RuntimeError("의도된 실패")

    draft = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=[_StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")]))],
            timeline_agent=_timeline_agent_returning_one_event(),
            repair_agent=confirm_only_repair_agent(),
            question_agent=_BoomQuestionAgent(),
        )
    )

    assert len(draft.events) == 1
    assert draft.events[0].question is None
    assert any("기록 질문" in w.message for w in draft.warnings)
    # 원본 예외 메시지는 draft 로 새지 않는다.
    assert all("의도된 실패" not in w.message for w in draft.warnings)



def test_timeline_agent_receives_candidate_places_from_the_input():
    """fan-in 이 candidate 에 입력 장소를 실어 Timeline Agent 로 넘긴다 (#72).

    Event Agent 는 이 필드를 채우지 않는다. 코드가 sourceRefs 로 입력을 찾아 복사하므로,
    Timeline Agent 가 받는 취합 결과에 장소 문자열이 이미 들어 있어야 한다.
    """

    seen: dict[str, AgentEventResult] = {}

    class _CapturingTimelineAgent(TimelineAgent):
        def generate(self, request, agent_result):
            seen["merged"] = agent_result
            return super().generate(request, agent_result)

    request = make_request(
        stays=[
            stay_item(
                1,
                raw_id="s-1",
                place="오산운암3단지 주공아파트",
                address="경기도 오산시 운암로 90",
                places=["강남파이낸스센터"],
            )
        ]
    )
    timeline = _CapturingTimelineAgent(
        llm=_timeline_agent_returning_one_event().llm
    )

    asyncio.run(
        run_main_agent(
            request,
            event_agents=[
                _StubAgent("a", AgentEventResult(candidates=[_candidate("s-1")]))
            ],
            timeline_agent=timeline,
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
        )
    )

    candidate = seen["merged"].candidates[0]
    assert candidate.place == "오산운암3단지 주공아파트"
    assert candidate.address == "경기도 오산시 운암로 90"
    # 후보를 줄이지 않는다. 고르는 것은 Timeline 의 일이다.
    assert candidate.places == ["오산운암3단지 주공아파트", "강남파이낸스센터"]
