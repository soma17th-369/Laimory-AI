"""세트별 eventType 경계 (#172).

`EventType` 은 모든 세트가 같이 쓴다. v4 는 17종이고 v1~v3 는 13종이다. v3 는 운영 세트이고
App Server 는 13종 밖의 값을 받으면 task 를 FAILED 로 만든다. 그래서 v1~v3 의 provider schema 는
한 글자도 달라지지 않고, LLM 이 새 값을 적어 내면 입구에서 모르는 문자열과 똑같이 거절한다.

`tests/fixtures/pre_clue_provider_schemas.json` 은 #172 직전 dev(`9c4f79f`)의 schema 다. v3 출력
계약을 일부러 바꾸는 변경이라면 이 파일도 같은 변경에서 고친다.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.parsing import parse_agent_result
from app.agents.repair.tools import execute_tool_calls
from app.agents.timeline.timeline_agent import parse_timeline_draft
from app.core.structured import to_strict_schema
from app.schemas import (
    PRE_CLUE_EVENT_TYPES,
    AgentEventResult,
    EventType,
    RepairToolCall,
    TimelineAgentOutput,
    TimelineClueOutput,
)
from tests.agents.test_repair_v4 import _context
from tests.agents.test_timeline_json_validation import _VALID_EVENT, _request, _response
from tests.fixtures.fake_llm import candidate

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre_clue_provider_schemas.json"

_V4_ONLY = ["STAY", "SHOPPING", "CULTURE", "ERRAND"]

_PROVIDER_SCHEMAS = {
    "TimelineAgentOutput": lambda: TimelineAgentOutput.model_json_schema(by_alias=True),
    "TimelineAgentOutput.strict": lambda: to_strict_schema(TimelineAgentOutput),
    "AgentEventResult": lambda: AgentEventResult.model_json_schema(by_alias=True),
    "AgentEventResult.strict": lambda: to_strict_schema(AgentEventResult),
}


def _dumped(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False)


# --- provider schema ---------------------------------------------------------------


@pytest.mark.parametrize("name", _PROVIDER_SCHEMAS)
def test_pre_clue_provider_schema_is_unchanged_to_the_letter(name):
    recorded = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    assert _dumped(_PROVIDER_SCHEMAS[name]()) == _dumped(recorded[name])


@pytest.mark.parametrize(
    "schema",
    [TimelineClueOutput.model_json_schema(by_alias=True), to_strict_schema(TimelineClueOutput)],
    ids=["plain", "strict"],
)
def test_v4_output_schema_opens_every_type(schema):
    prop = schema["$defs"]["TimelineClueEvent"]["properties"]["eventType"]

    assert prop["enum"] == [member.value for member in EventType]


def test_the_pre_clue_set_is_what_the_app_server_takes_today():
    assert len(PRE_CLUE_EVENT_TYPES) == 13
    assert {member.value for member in set(EventType) - PRE_CLUE_EVENT_TYPES} == set(_V4_ONLY)


# --- 입구 세 곳 ----------------------------------------------------------------------


@pytest.mark.parametrize("value", _V4_ONLY)
def test_timeline_parse_treats_a_new_type_like_an_unknown_string(value):
    def parsed(event_type):
        return parse_timeline_draft(
            _response([{**_VALID_EVENT, "eventType": event_type}, _VALID_EVENT]), _request()
        )

    draft, unknown = parsed(value), parsed("NOT_A_TYPE")

    assert [event.title for event in draft.events] == ["정상 이벤트"]
    assert [event.client_event_id for event in draft.events] == [
        event.client_event_id for event in unknown.events
    ]
    assert [warning.message for warning in draft.warnings] == [
        warning.message for warning in unknown.warnings
    ]


@pytest.mark.parametrize("value", [*_V4_ONLY, "NOT_A_TYPE"])
def test_event_agent_result_rejects_a_new_type(value):
    text = json.dumps(
        {
            "candidates": [
                candidate(
                    value,
                    [("STAY", "stay-1")],
                    start="2026-06-20T09:00:00+09:00",
                    end="2026-06-20T10:00:00+09:00",
                )
            ],
            "fragments": [],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValidationError):
        parse_agent_result(text)


def _update(value) -> RepairToolCall:
    return RepairToolCall(
        tool="update_event",
        args={"clientEventId": "event-001", "fields": {"eventType": value}},
        reason="타입을 바꾼다",
    )


def _split(value) -> RepairToolCall:
    return RepairToolCall(
        tool="split_event",
        args={
            "clientEventId": "event-001",
            "parts": [
                {
                    "startTime": "2026-06-20T09:00:00+09:00", "endTime": "2026-06-20T12:00:00+09:00",
                    "eventType": value, "title": "앞",
                },
                {
                    "startTime": "2026-06-20T12:00:00+09:00", "endTime": "2026-06-20T18:00:00+09:00",
                    "eventType": "WORK", "title": "뒤",
                },
            ],
        },
        reason="나눈다",
    )


@pytest.mark.parametrize("call", [_update, _split], ids=["update_event", "split_event"])
@pytest.mark.parametrize("value", _V4_ONLY)
def test_pre_clue_repair_tools_reject_a_new_type(call, value):
    ctx, _ = _context()
    ctx.clues = None
    before = ctx.draft.model_dump(by_alias=True, mode="json")

    [result] = execute_tool_calls(ctx, [call(value)])

    assert not result.ok and value in result.message
    assert ctx.draft.model_dump(by_alias=True, mode="json") == before


def test_pre_clue_repair_keeps_rejecting_a_value_that_is_not_a_string():
    ctx, _ = _context()
    ctx.clues = None

    [result] = execute_tool_calls(ctx, [_update(["STAY"])])

    assert not result.ok


@pytest.mark.parametrize("value", _V4_ONLY)
def test_v4_repair_can_use_the_new_types(value):
    ctx, _ = _context()

    [result] = execute_tool_calls(ctx, [_update(value)])

    assert result.ok, result.message
    assert ctx.draft.events[0].event_type.value == value
