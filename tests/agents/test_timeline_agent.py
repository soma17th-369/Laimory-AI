"""Timeline Agent tests."""

import json
from pathlib import Path

from app.agents.timeline_agent import TimelineAgent, parse_timeline_draft
from app.schemas import (
    AgentEventResult,
    AgentWarning,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraftRequest,
    TimeWindow,
)
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import make_request

DATA_DIR = Path("data")
INPUT_DIR = DATA_DIR / "input"
EXPECTED_OUTPUT = DATA_DIR / "output" / "timeline-draft.expected.json"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _draft_json(events=None, warnings=None, questions=None) -> str:
    return json.dumps(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": events or [],
            "questions": questions or [],
            "warnings": warnings or [],
        },
        ensure_ascii=False,
    )


def _event_obj(
    source_ids,
    *,
    event_type="STAY",
    title="병합 이벤트",
    description="설명",
    start="2026-06-20T09:00:00+09:00",
    end="2026-06-20T11:00:00+09:00",
    inference_level="EVIDENCE_BASED",
    uncertainty=None,
):
    return {
        "clientEventId": "hallucinated",
        "eventType": event_type,
        "title": title,
        "description": description,
        "startTime": start,
        "endTime": end,
        "confidence": 0.85,
        "inferenceLevel": inference_level,
        "sourceRefs": [
            {"sourceType": "LOCATION", "sourceId": sid} for sid in source_ids
        ],
        "uncertainty": uncertainty or [],
    }


def _candidate(
    source_id,
    *,
    event_type=EventType.STAY,
    start="2026-06-20T09:00:00+09:00",
    end="2026-06-20T10:00:00+09:00",
):
    return AiEventCandidate(
        event_type=event_type,
        time_range=CandidateTimeRange(start_time=start, end_time=end),
        title="후보",
        description="설명",
        source_refs=[
            SourceRef(source_type=EventSourceType.LOCATION, source_id=source_id)
        ],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        uncertainty=[],
    )


def _request_from_input_fixture() -> TimelineDraftRequest:
    payload = _load_json(INPUT_DIR / "test2026-06-30.json")
    return TimelineDraftRequest(
        transaction_id="tx-test2026-06-30",
        date=payload["date"],
        mode=payload["mode"],
        window=TimeWindow(**payload["window"]),
        generated_at=payload["generatedAt"],
    )


def _agent_result_from_expected_fixture() -> AgentEventResult:
    expected = _load_json(EXPECTED_OUTPUT)
    candidates = []
    for event in expected["events"]:
        first_ref = event["sourceRefs"][0]
        candidates.append(
            AiEventCandidate(
                event_type=event["eventType"],
                time_range=CandidateTimeRange(
                    start_time=event["startTime"],
                    end_time=event["endTime"],
                ),
                title=event["title"],
                description=event["description"],
                source_refs=[
                    SourceRef(
                        source_type=ref["sourceType"],
                        source_id=ref["sourceId"],
                    )
                    for ref in event["sourceRefs"]
                ],
                confidence=event["confidence"],
                inference_level=event["inferenceLevel"],
                uncertainty=event["uncertainty"],
            )
        )
        assert first_ref["sourceId"]
    return AgentEventResult(candidates=candidates)


def test_empty_result_skips_llm_and_carries_warnings():
    fake = FakeLLM([_draft_json()])
    upstream = AgentEventResult(
        warnings=[AgentWarning(agent_name="photo", message="사진 분석 일부 실패")]
    )

    draft = TimelineAgent(llm=fake).generate(make_request(), upstream)

    assert fake.calls == []
    assert draft.user_id == "user-1234"
    assert draft.date == "2026-06-20"
    assert draft.timezone == "Asia/Seoul"
    assert draft.events == []
    assert draft.questions == []
    assert len(draft.warnings) == 1
    assert draft.warnings[0].warning_id == "warning-upstream-001"
    assert draft.warnings[0].message == "[photo] 사진 분석 일부 실패"


def test_merges_candidates_and_parses_draft():
    response = _draft_json(
        events=[_event_obj(["stay-1", "stay-2"])],
        warnings=[{"message": "오후 위치 기록이 약합니다"}],
        questions=[
            {
                "timeRange": {
                    "startTime": "2026-06-20T12:00:00+09:00",
                    "endTime": "2026-06-20T12:30:00+09:00",
                },
                "question": "12시에 겹치는 일정 중 어떤 것이 맞나요?",
                "reason": "캘린더와 위치 기록이 겹칩니다.",
                "relatedEventIds": [],
            }
        ],
    )
    fake = FakeLLM([response])
    upstream = AgentEventResult(
        candidates=[_candidate("stay-1"), _candidate("stay-2")],
        warnings=[AgentWarning(agent_name="sleep_activity", message="수면 누락")],
    )

    draft = TimelineAgent(llm=fake).generate(make_request(), upstream)

    assert len(fake.calls) == 1
    assert "Timeline Agent" in fake.calls[0].system
    assert "stay-1" in fake.calls[0].prompt
    assert fake.calls[0].prompt.index("AI Event candidates") < fake.calls[0].prompt.index(
        "Source fragments"
    )

    assert len(draft.events) == 1
    event = draft.events[0]
    assert event.client_event_id == "event-001"
    assert event.event_type is EventType.STAY
    assert event.description == "설명"
    assert [r.source_id for r in event.source_refs] == ["stay-1", "stay-2"]

    assert draft.questions[0].question_id == "question-001"
    assert draft.warnings[0].warning_id == "warning-upstream-001"
    assert draft.warnings[1].warning_id == "warning-001"


def test_llm_failure_returns_schema_complete_warning_result():
    fake = FakeLLM([RuntimeError("timeline llm down")])
    upstream = AgentEventResult(
        candidates=[_candidate("stay-1")],
        warnings=[AgentWarning(agent_name="location", message="위치 일부 실패")],
    )

    draft = TimelineAgent(llm=fake).generate(make_request(), upstream)

    assert draft.user_id == "user-1234"
    assert draft.date == "2026-06-20"
    assert draft.timezone == "Asia/Seoul"
    assert draft.events == []
    messages = [w.message for w in draft.warnings]
    assert "[location] 위치 일부 실패" in messages
    assert any("timeline llm down" in message for message in messages)


def test_parse_assigns_sequential_client_event_ids_ignoring_llm_ids():
    text = _draft_json(
        events=[
            {**_event_obj(["stay-1"]), "clientEventId": "hallucinated"},
            _event_obj(["stay-2"], title="두 번째"),
        ]
    )

    draft = parse_timeline_draft(text)

    assert [event.client_event_id for event in draft.events] == [
        "event-001",
        "event-002",
    ]


def test_timeline_agent_matches_data_input_output_fixture():
    input_files = sorted(INPUT_DIR.glob("*.json"))
    assert {path.name for path in input_files} == {
        "test2026-06-30.calendar.json",
        "test2026-06-30.health.json",
        "test2026-06-30.json",
        "test2026-06-30.location.json",
        "test2026-06-30.notifications.json",
        "test2026-06-30.photos.json",
    }
    for path in input_files:
        _load_json(path)

    expected = _load_json(EXPECTED_OUTPUT)
    request = _request_from_input_fixture()
    upstream = _agent_result_from_expected_fixture()
    fake = FakeLLM([json.dumps(expected, ensure_ascii=False)])

    draft = TimelineAgent(llm=fake).generate(request, upstream)

    assert fake.calls
    assert "2026-06-30" in fake.calls[0].prompt
    assert "location-stay-001" in fake.calls[0].prompt
    assert draft.model_dump(by_alias=True, mode="json") == expected
