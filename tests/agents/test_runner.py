"""테스트용 Agent 취합 helper 검증."""

from app.agents.base import Agent
from app.agents.events import default_event_agents
from app.schemas import (
    AgentEventResult,
    AgentWarning,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceFragment,
    SourceRef,
)
from tests.fixtures.requests import make_request


def _generate_all_for_test(request, agents):
    """테스트 안에서만 쓰는 단순 취합 helper.

    실전 실행 순서와 실패 정책은 pipeline/timeline agent 에서 정한다. 여기서는 각
    Agent 반환 계약(candidates/fragments/warnings)이 합쳐질 수 있는지만 검증한다.
    """

    combined = AgentEventResult()
    for agent in agents:
        result = agent.generate(request)
        combined.candidates.extend(result.candidates)
        combined.fragments.extend(result.fragments)
        combined.warnings.extend(result.warnings)
    return combined


def _candidate(source_id):
    return AiEventCandidate(
        event_type=EventType.STAY,
        time_range=CandidateTimeRange(
            start_time="2026-06-20T09:00:00+09:00",
            end_time="2026-06-20T10:00:00+09:00",
        ),
        title="t",
        description="d",
        source_refs=[SourceRef(source_type=EventSourceType.LOCATION, source_id=source_id)],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        uncertainty=["x"],
    )


class _StubAgent(Agent):
    def __init__(self, name, result):
        self.name = name
        self._result = result

    def generate(self, request):
        return self._result


def test_default_event_agents_cover_all_sources():
    names = {a.name for a in default_event_agents()}
    assert names == {"location", "calendar", "photo", "sleep_activity", "notification"}


def test_test_helper_merges_candidates_fragments_and_warnings():
    stub1 = _StubAgent(
        "a", AgentEventResult(candidates=[_candidate("s-1")], fragments=[])
    )
    stub2 = _StubAgent(
        "b",
        AgentEventResult(
            candidates=[],
            fragments=[
                SourceFragment(
                    source_type=EventSourceType.PHOTO,
                    source_id="p-1",
                    summary="사진",
                )
            ],
            warnings=[AgentWarning(agent_name="photo", message="일부 실패")],
        ),
    )

    result = _generate_all_for_test(make_request(), agents=[stub1, stub2])

    assert [c.source_refs[0].source_id for c in result.candidates] == ["s-1"]
    assert [f.source_id for f in result.fragments] == ["p-1"]
    assert [f.summary for f in result.fragments] == ["사진"]
    assert [w.agent_name for w in result.warnings] == ["photo"]
