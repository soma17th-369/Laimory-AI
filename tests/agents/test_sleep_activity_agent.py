"""Sleep/Activity Event Agent 검증 (LLM 은 fake 주입)."""

from app.agents.events.sleep_activity import SleepActivityEventAgent
from app.schemas import EventType
from tests.fixtures.fake_llm import FakeLLM, candidate, fragment, result_json
from tests.fixtures.requests import (
    DAY_START,
    HOUR,
    health_data,
    make_request,
    sleep,
    steps,
)


def test_empty_health_skips_llm():
    fake = FakeLLM([result_json()])
    result = SleepActivityEventAgent(llm=fake).generate(make_request())
    assert result.candidates == []
    assert result.fragments == []
    assert fake.calls == []


def test_missing_health_skips_llm():
    fake = FakeLLM([result_json()])
    req = make_request().model_copy(update={"health": None})
    result = SleepActivityEventAgent(llm=fake).generate(req)
    assert result.candidates == []
    assert result.fragments == []
    assert result.warnings == []
    assert fake.calls == []


def test_sleep_event_and_activity_fragment():
    final = result_json(
        candidates=[
            candidate(
                "SLEEP",
                [("SLEEP", "sleep-1")],
                inference_level="DIRECT",
                confidence=0.9,
            )
        ],
        fragments=[fragment("ACTIVITY", "steps-1", "걸음 수 8000보")],
    )
    fake = FakeLLM([result_json(), final])
    req = make_request(
        health=health_data(
            sleep=sleep("sleep-1", DAY_START, DAY_START + 7 * HOUR),
            steps=steps("steps-1", 8000),
        )
    )

    result = SleepActivityEventAgent(llm=fake).generate(req)

    # infer → review 2단계 호출(graph agent).
    assert len(fake.calls) == 2
    assert "라이프로그" in fake.calls[0].system
    # 수면·활동 데이터가 모두 프롬프트에 들어간다.
    assert "sleep-1" in fake.calls[0].prompt
    assert "steps-1" in fake.calls[0].prompt
    assert result.candidates[0].event_type is EventType.SLEEP
    assert result.fragments[0].source_id == "steps-1"
