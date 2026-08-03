"""Timeline Agent 의 JSON/시간 검증 강화 + fallback 전략 검증.

- 개별 event 가 스키마(ISO datetime·timezone·start/end 순서) 검증에 실패하면
  그 event 만 제외하고 나머지는 살린다(warning 남김).
- 응답이 아예 유효한 JSON 이 아니면 빈 fallback draft 로 처리한다.
"""

import json
from pathlib import Path

from app.agents.prompt_loader import load_prompt
from app.agents.timeline.timeline_agent import TimelineAgent
from app.schemas import AgentEventResult
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


def _response(events) -> str:
    return json.dumps(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": events,
            "questions": [],
            "warnings": [],
        },
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
