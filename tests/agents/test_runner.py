"""통합 러너 generate_all 검증."""

from app.agents.base import Agent
from app.agents.events import default_event_agents, generate_all
from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceFragment,
    SourceRef,
)
from tests.fixtures.requests import make_request


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


def test_generate_all_merges_candidates_and_fragments():
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
        ),
    )

    result = generate_all(make_request(), agents=[stub1, stub2])

    assert [c.source_refs[0].source_id for c in result.candidates] == ["s-1"]
    assert [f.source_id for f in result.fragments] == ["p-1"]
    assert [f.summary for f in result.fragments] == ["사진"]
