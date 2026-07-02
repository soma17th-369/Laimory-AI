"""Calendar Event Agent 검증 (LLM 은 fake 주입)."""

from app.agents.events.calendar import CalendarEventAgent
from app.schemas import EventSourceType, EventType, InferenceLevel
from tests.fixtures.fake_llm import FakeLLM, candidate, fragment, result_json
from tests.fixtures.requests import (
    DAY_START,
    HOUR,
    calendar_data,
    calendar_event,
    make_request,
)


def test_empty_calendar_skips_llm():
    fake = FakeLLM([result_json()])
    result = CalendarEventAgent(llm=fake).generate(make_request())
    assert result.candidates == []
    assert result.fragments == []
    assert fake.calls == []


def test_calendar_infers_direct_event():
    final = result_json(
        candidates=[
            candidate(
                "CALENDAR_EVENT",
                [("CALENDAR", "cal-1")],
                confidence=0.95,
                inference_level="DIRECT",
            )
        ],
        fragments=[fragment("CALENDAR", "cal-1", "멘토링 일정 요약")],
    )
    fake = FakeLLM([final])
    req = make_request(
        calendar=calendar_data(
            events=[calendar_event("cal-1", "멘토링", DAY_START, DAY_START + HOUR)]
        )
    )

    result = CalendarEventAgent(llm=fake).generate(req)

    # 단일 호출 agent.
    assert len(fake.calls) == 1
    assert "라이프로그" in fake.calls[0].system
    assert "cal-1" in fake.calls[0].prompt
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.event_type is EventType.CALENDAR_EVENT
    assert cand.inference_level is InferenceLevel.DIRECT
    assert len(result.fragments) == 1
    assert result.fragments[0].source_type is EventSourceType.CALENDAR
    assert result.fragments[0].source_id == "cal-1"
    assert result.fragments[0].summary == "멘토링 일정 요약"


def test_calendar_failure_returns_warning_result():
    fake = FakeLLM([RuntimeError("llm down")])
    req = make_request(
        calendar=calendar_data(
            events=[calendar_event("cal-1", "멘토링", DAY_START, DAY_START + HOUR)]
        )
    )

    result = CalendarEventAgent(llm=fake).generate(req)

    assert result.candidates == []
    assert result.fragments == []
    assert len(result.warnings) == 1
    assert result.warnings[0].agent_name == "calendar"
    assert "llm down" in result.warnings[0].message
