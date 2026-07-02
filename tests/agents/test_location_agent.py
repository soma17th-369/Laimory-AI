"""Location Event Agent 검증 (LLM 은 fake 주입)."""

from app.agents.events.location import LocationEventAgent
from app.schemas import EventSourceType, EventType, UserMemory
from tests.fixtures.fake_llm import FakeLLM, candidate, fragment, result_json
from tests.fixtures.requests import (
    DAY_START,
    HOUR,
    location_data,
    make_request,
    stay,
)


def test_empty_location_skips_llm():
    fake = FakeLLM([result_json()])
    result = LocationEventAgent(llm=fake).generate(make_request())
    assert result.candidates == []
    assert result.fragments == []
    assert fake.calls == []  # 데이터 없으면 LLM 을 부르지 않는다.


def test_location_infers_from_data_and_user_memory():
    final = result_json(
        candidates=[candidate("STAY", [("LOCATION", "stay-1")])],
        fragments=[fragment("LOCATION", "stay-1", "위치상 한 장소에 체류한 기록")],
    )
    fake = FakeLLM([result_json(), final])
    req = make_request(
        location=location_data(
            stays=[stay("stay-1", 37.5, 127.0, DAY_START + 9 * HOUR, DAY_START + 10 * HOUR)]
        ),
        user_memory=UserMemory(job="student"),
    )

    result = LocationEventAgent(llm=fake).generate(req)

    # infer → review 2단계 호출(graph agent).
    assert len(fake.calls) == 2
    # prompt.md 가 system 으로 연결된다.
    assert "라이프로그" in fake.calls[0].system
    # infer 프롬프트에 데이터 sourceId 와 user memory 가 포함된다.
    assert "stay-1" in fake.calls[0].prompt
    assert "student" in fake.calls[0].prompt
    # review 프롬프트에는 1단계 초안이 들어간다.
    assert "candidates" in fake.calls[1].prompt
    # 최종 응답이 스키마로 파싱된다.
    assert len(result.candidates) == 1
    assert result.candidates[0].event_type is EventType.STAY
    assert result.candidates[0].source_refs[0].source_id == "stay-1"
    assert len(result.fragments) == 1
    assert result.fragments[0].source_type is EventSourceType.LOCATION
    assert result.fragments[0].source_id == "stay-1"
    assert result.fragments[0].summary == "위치상 한 장소에 체류한 기록"


def test_location_graph_failure_returns_warning_result():
    fake = FakeLLM([RuntimeError("graph llm down")])
    req = make_request(
        location=location_data(
            stays=[stay("stay-1", 37.5, 127.0, DAY_START + 9 * HOUR, DAY_START + 10 * HOUR)]
        )
    )

    result = LocationEventAgent(llm=fake).generate(req)

    assert result.candidates == []
    assert result.fragments == []
    assert len(result.warnings) == 1
    assert result.warnings[0].agent_name == "location"
    assert "graph llm down" in result.warnings[0].message
