"""Timeline Agent 의 JSON/시간 검증 강화 + fallback 전략 검증.

- 개별 event 가 스키마(ISO datetime·timezone·start/end 순서) 검증에 실패하면
  그 event 만 제외하고 나머지는 살린다(warning 남김).
- 응답이 아예 유효한 JSON 이 아니면 빈 fallback draft 로 처리한다.
"""

import json
from pathlib import Path

from app.agents.prompt_loader import load_prompt
from app.agents.timeline.timeline_agent import TimelineAgent, parse_timeline_draft
from app.core.structured import to_strict_schema
from app.schemas import AgentEventResult, TimelineAgentOutput, TimelineDraft
from tests.fixtures.fake_llm import FakeLLM, candidate
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


def _request():
    """draft 가 참조하는 rawId 를 담은 요청(sourceRef 검증을 통과하도록)."""

    return make_request(stays=[stay_item(1, raw_id="stay-1")])


_VALID_EVENT = {
    "eventType": "REST",
    "title": "정상 이벤트",
    "description": "",
    "startTime": "2026-06-20T09:00:00+09:00",
    "endTime": "2026-06-20T10:00:00+09:00",
    "confidence": 0.7,
    "inferenceLevel": "EVIDENCE_BASED",
    "sourceRefs": [
        {"sourceType": "STAY", "rawId": fixture_raw_id("stay-1")}
    ],
    "uncertainty": [],
}


def _agent_result() -> AgentEventResult:
    return AgentEventResult.model_validate(
        {
            "candidates": [
                candidate(
                    "REST",
                    [("STAY", "stay-1")],
                    start="2026-06-20T09:00:00+09:00",
                    end="2026-06-20T10:00:00+09:00",
                )
            ],
            "fragments": [],
        }
    )


def _response(events, **extra) -> str:
    return json.dumps(
        {"events": events, "warnings": [], **extra},
        ensure_ascii=False,
    )


def test_timeline_prompt_allows_one_source_to_support_multiple_events():
    module_file = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "agents"
        / "timeline"
        / "timeline_agent.py"
    )
    prompt = load_prompt(module_file, "timeline.md")

    assert "각 event가 그 source를 함께 참조할 수 있습니다" in prompt
    assert "하나의 `rawId`는 오직 하나의 event" not in prompt


def test_invalid_event_is_dropped_others_survive():
    # 두 번째 event 는 end < start 라 스키마 검증에 실패한다.
    bad_order = {**_VALID_EVENT, "title": "시간 역전", "startTime": "2026-06-20T11:00:00+09:00", "endTime": "2026-06-20T10:00:00+09:00"}
    response = _response([_VALID_EVENT, bad_order])

    draft = TimelineAgent(llm=FakeLLM([response])).generate(_request(), _agent_result())

    assert len(draft.events) == 1
    assert draft.events[0].title == "정상 이벤트"
    assert draft.events[0].client_event_id == "event-001"
    assert any("형식 검증" in w.message for w in draft.warnings)


def test_naive_datetime_event_is_dropped():
    # timezone 없는(naive) datetime 은 AwareDatetime 검증에 실패한다.
    naive = {**_VALID_EVENT, "startTime": "2026-06-20T09:00:00", "endTime": "2026-06-20T10:00:00"}
    response = _response([naive])

    draft = TimelineAgent(llm=FakeLLM([response])).generate(_request(), _agent_result())

    assert draft.events == []
    assert any("형식 검증" in w.message for w in draft.warnings)


def test_invalid_json_falls_back_to_empty_draft():
    draft = TimelineAgent(llm=FakeLLM(["{ this is not valid json ]"])).generate(
        make_request(), _agent_result()
    )

    assert draft.events == []
    assert draft.date == "2026-06-20"
    assert any("실행 실패" in w.message for w in draft.warnings)
    assert all("Expecting" not in w.message for w in draft.warnings)


# --- LLM 출력 계약 (#118) -------------------------------------------------------


def test_llm_output_contract_has_only_events_and_warnings():
    """내부 모호성 질문은 없앴다. 코드가 채우는 clientEventId·question 도 LLM 몫이 아니다."""

    assert set(TimelineAgentOutput.model_fields) == {"events", "warnings"}
    event_fields = {
        field.alias or name
        for name, field in TimelineAgentOutput.model_fields["events"].annotation.__args__[0].model_fields.items()
    }
    assert "clientEventId" not in event_fields
    assert "question" not in event_fields
    assert "questions" not in TimelineDraft.model_fields


def test_llm_output_contract_can_be_expressed_as_a_strict_schema():
    """자유형 object 가 없어야 OpenAI strict 모드가 형태를 잠근다.

    옛 `TimelineQuestion.timeRange` 가 자유형 dict 라 draft 전체를 실을 때는 json_object
    모드로 떨어졌다.
    """

    assert to_strict_schema(TimelineAgentOutput) is not None


def test_legacy_questions_key_from_an_older_prompt_is_ignored():
    """v2 프롬프트는 여전히 questions 를 내라고 한다. 값은 버려지고 event 는 산다."""

    legacy = {
        "timeRange": {
            "startTime": "2026-06-20T09:00:00+09:00",
            "endTime": "2026-06-20T10:00:00+09:00",
        },
        "question": "시간 확인 필요",
        "reason": "위치 기록만 있음",
        "relatedEventIds": ["event-001"],
    }

    draft = parse_timeline_draft(_response([_VALID_EVENT], questions=[legacy]), _request())

    assert [event.title for event in draft.events] == ["정상 이벤트"]
    assert not hasattr(draft, "questions")
    assert draft.warnings == []
