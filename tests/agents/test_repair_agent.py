"""Repair Agent 검증.

보는 것은 네 가지다.

    1. 코드 확정은 LLM 이 무엇을 하든 항상 지나간다(정렬·clientEventId 재부여).
    2. LLM 이 낸 계획이 도구로 실행되어 draft 에 반영된다.
    3. 반복은 `done` 이나 상한에서 멈춘다.
    4. 실패해도 draft 를 잃지 않는다(직전 확정 draft + warning).

LLM 은 `FakeLLM` 으로 대신하고, 실제 호출은 하지 않는다.
"""

import json

from app.agents.repair import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.observability import (
    ContentCapture,
    InMemoryObservationSink,
    ObservationEventType,
    ObservationStage,
    Observer,
    observation_context,
    observation_scope,
)
from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.pipeline import StubEventAgent
from tests.fixtures.requests import make_request, stay_item


def _request():
    """서로 다른 장소의 체류 두 건.

    같은 장소로 두면 `stay_merge` 가 "이동 없이 이어진 한 체류" 로 보고 두 event 를
    하나로 합친다. 여기서 보려는 것은 그 병합이 아니므로 장소를 갈라 둔다.
    """

    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="s-1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T10:00:00",
                place="카페",
                address="서울 카페 주소",
                places=["카페"],
            ),
            stay_item(
                2,
                raw_id="s-2",
                start="2026-06-20T15:00:00",
                end="2026-06-20T16:00:00",
                lat=37.6,
                lon=127.2,
                place="사무실",
                address="서울 사무실 주소",
                places=["사무실"],
            ),
        ]
    )


def _event(title: str, start: str, end: str, raw_id: str = "s-1", client_event_id: str = "event-001"):
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=EventType.REST,
        title=title,
        description="설명",
        start_time=f"2026-06-20T{start}:00+09:00",
        end_time=f"2026-06-20T{end}:00+09:00",
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, source_id=raw_id)],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=list(events),
    )


def _plan(tool_calls: list[dict], *, done: bool = False, issues: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "issues": issues or [],
            "toolCalls": tool_calls,
            "done": done,
            "summary": "요약",
        },
        ensure_ascii=False,
    )


_NOTHING_TO_FIX = _plan([], done=True)


# --- 코드 확정 ----------------------------------------------------------------


def test_confirms_draft_even_when_llm_finds_nothing():
    """LLM 이 고칠 게 없다고 해도 정렬과 id 부여는 코드가 한다."""

    draft = _draft(
        _event("오후", "15:00", "16:00", raw_id="s-2", client_event_id="event-001"),
        _event("오전", "09:00", "10:00", raw_id="s-1", client_event_id="event-002"),
    )
    agent = RepairAgent(llm=FakeLLM([_NOTHING_TO_FIX]), max_iterations=2)

    result = agent.generate(_request(), draft)

    assert [event.title for event in result.events] == ["오전", "오후"]
    assert [event.client_event_id for event in result.events] == ["event-001", "event-002"]


def test_zero_iterations_skips_llm_entirely():
    """반복 상한이 0 이면 LLM 없이 결정론 확정만 한다."""

    llm = FakeLLM([_NOTHING_TO_FIX])
    draft = _draft(
        _event("오후", "15:00", "16:00", raw_id="s-2", client_event_id="event-001"),
        _event("오전", "09:00", "10:00", raw_id="s-1", client_event_id="event-002"),
    )

    result = RepairAgent(llm=llm, max_iterations=0).generate(_request(), draft)

    assert llm.calls == []
    assert [event.title for event in result.events] == ["오전", "오후"]


# --- 도구 실행 ----------------------------------------------------------------


def test_applies_update_event_from_plan():
    plan = _plan(
        [
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-001",
                    "fields": {"title": "카페에서 쉬었다"},
                },
                "reason": "데이터 라벨 같은 제목",
            }
        ],
        done=True,
    )
    agent = RepairAgent(llm=FakeLLM([plan]), max_iterations=2)

    result = agent.generate(_request(), _draft(_event("체류", "09:00", "10:00")))

    assert result.events[0].title == "카페에서 쉬었다"
    # 지정하지 않은 필드는 그대로다.
    assert result.events[0].start_time.hour == 9


def test_repair_observes_plan_tool_result_and_each_confirmed_timeline():
    plan = _plan(
        [
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-001",
                    "fields": {"title": "관측된 수정"},
                },
            }
        ],
        done=True,
    )
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)
    request = _request()

    with observation_context(request.transaction_id, observer):
        with observation_scope(ObservationStage.REPAIR_AGENT, agent="repair"):
            RepairAgent(llm=FakeLLM([plan]), max_iterations=2).generate(
                request,
                _draft(_event("체류", "09:00", "10:00")),
            )

    types = [event.event_type for event in sink.events]
    assert ObservationEventType.PLAN in types
    assert ObservationEventType.TOOL_CALL in types
    assert types.count(ObservationEventType.DRAFT_UPDATED) == 2
    plan_event = next(
        event for event in sink.events if event.event_type is ObservationEventType.PLAN
    )
    tool_event = next(
        event
        for event in sink.events
        if event.event_type is ObservationEventType.TOOL_CALL
    )
    assert plan_event.iteration == 1
    assert tool_event.iteration == 1
    assert tool_event.payload["call"]["tool"] == "update_event"
    assert tool_event.payload["timeline"]["events"][0]["title"] == "관측된 수정"


def test_applies_delete_event_and_renumbers():
    plan = _plan(
        [{"tool": "delete_event", "args": {"clientEventId": "event-001"}}],
        done=True,
    )
    draft = _draft(
        _event("오전", "09:00", "10:00", raw_id="s-1", client_event_id="event-001"),
        _event("오후", "15:00", "16:00", raw_id="s-2", client_event_id="event-002"),
    )

    result = RepairAgent(llm=FakeLLM([plan]), max_iterations=2).generate(_request(), draft)

    assert [event.title for event in result.events] == ["오후"]
    # 삭제로 생긴 번호 구멍은 확정 단계가 메운다.
    assert [event.client_event_id for event in result.events] == ["event-001"]


def test_unknown_tool_is_reported_back_and_draft_survives():
    llm = FakeLLM(
        [
            _plan([{"tool": "sort_events", "args": {}}]),  # 카탈로그에 없는 도구
            _NOTHING_TO_FIX,
        ]
    )

    result = RepairAgent(llm=llm, max_iterations=2).generate(
        _request(), _draft(_event("체류", "09:00", "10:00"))
    )

    assert result.events[0].title == "체류"
    # 실패한 호출은 다음 분석 프롬프트에 그대로 실려 LLM 이 다시 판단하게 한다.
    assert "없는 도구입니다" in llm.calls[1].prompt


def test_failed_tool_call_does_not_stop_the_rest_of_the_plan():
    plan = _plan(
        [
            {"tool": "update_event", "args": {"clientEventId": "event-999", "fields": {"title": "x"}}},
            {"tool": "update_event", "args": {"clientEventId": "event-001", "fields": {"title": "고침"}}},
        ],
        done=True,
    )

    result = RepairAgent(llm=FakeLLM([plan]), max_iterations=2).generate(
        _request(), _draft(_event("체류", "09:00", "10:00"))
    )

    assert result.events[0].title == "고침"


# --- 반복 제어 ----------------------------------------------------------------


def test_stops_at_iteration_limit():
    """계획이 끝없이 이어져도 상한에서 멈춘다."""

    never_done = _plan(
        [
            {
                "tool": "update_event",
                "args": {"clientEventId": "event-001", "fields": {"title": "고침"}},
            }
        ]
    )
    llm = FakeLLM([never_done])  # 응답이 떨어지면 마지막 응답을 반복한다

    result = RepairAgent(llm=llm, max_iterations=2).generate(
        _request(), _draft(_event("체류", "09:00", "10:00"))
    )

    assert len(llm.calls) == 2
    assert result.events[0].title == "고침"


def test_stops_when_plan_has_no_tool_calls():
    llm = FakeLLM([_NOTHING_TO_FIX])

    RepairAgent(llm=llm, max_iterations=3).generate(
        _request(), _draft(_event("체류", "09:00", "10:00"))
    )

    assert len(llm.calls) == 1


# --- 실패 fallback -------------------------------------------------------------


def test_unparseable_response_keeps_confirmed_draft():
    llm = FakeLLM(["미안하지만 JSON 을 못 만들겠어요"])
    draft = _draft(
        _event("오후", "15:00", "16:00", raw_id="s-2", client_event_id="event-001"),
        _event("오전", "09:00", "10:00", raw_id="s-1", client_event_id="event-002"),
    )

    result = RepairAgent(llm=llm, max_iterations=2).generate(_request(), draft)

    # 개선은 못 했지만 확정된 draft 는 그대로 살아 있다.
    assert [event.title for event in result.events] == ["오전", "오후"]
    assert any("개선 실패" in warning.message for warning in result.warnings)


def test_llm_failure_keeps_improvements_from_earlier_iterations():
    llm = FakeLLM(
        [
            _plan(
                [
                    {
                        "tool": "update_event",
                        "args": {"clientEventId": "event-001", "fields": {"title": "고침"}},
                    }
                ]
            ),
            RuntimeError("LLM 장애"),
        ]
    )

    result = RepairAgent(llm=llm, max_iterations=3).generate(
        _request(), _draft(_event("체류", "09:00", "10:00"))
    )

    # 1차 개선은 남고, 2차 실패는 warning 으로만 남는다.
    assert result.events[0].title == "고침"
    assert any("개선 실패" in warning.message for warning in result.warnings)


# --- 상류 Agent 재실행 ---------------------------------------------------------


def _candidate(title: str, raw_id: str = "s-1") -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.REST,
        time_range=CandidateTimeRange(
            start_time="2026-06-20T09:00:00+09:00",
            end_time="2026-06-20T10:00:00+09:00",
        ),
        title=title,
        description="설명",
        source_refs=[SourceRef(source_type=EventSourceType.STAY, source_id=raw_id)],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
    )


def _timeline_draft_json(title: str) -> str:
    return json.dumps(
        {
            "userId": "u",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                {
                    "eventType": "REST",
                    "title": title,
                    "description": "",
                    "startTime": "2026-06-20T09:00:00+09:00",
                    "endTime": "2026-06-20T10:00:00+09:00",
                    "confidence": 0.8,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [{"sourceType": "STAY", "rawId": "s-1"}],
                }
            ],
        },
        ensure_ascii=False,
    )


def test_reruns_event_agent_and_timeline_agent():
    """한 source 의 해석이 통째로 잘못됐을 때 상류 Agent 를 다시 돌린다."""

    rerun_result = AgentEventResult(candidates=[_candidate("다시 뽑은 후보")])
    location_agent = StubEventAgent(rerun_result, name="location")
    timeline_agent = TimelineAgent(llm=FakeLLM([_timeline_draft_json("다시 만든 초안")]))

    plan = _plan(
        [
            {"tool": "rerun_event_agent", "args": {"agent": "location"}},
            {"tool": "rerun_timeline_agent", "args": {}},
        ],
        done=True,
    )
    agent = RepairAgent(llm=FakeLLM([plan]), max_iterations=2)

    result = agent.generate(
        _request(),
        _draft(_event("잘못된 초안", "09:00", "10:00")),
        event_results={"location": AgentEventResult(candidates=[_candidate("처음 후보")])},
        event_agents={"location": location_agent},
        timeline_agent=timeline_agent,
    )

    assert [event.title for event in result.events] == ["다시 만든 초안"]
    assert result.events[0].client_event_id == "event-001"

    # Timeline Agent 는 "다시 돌린 Agent 의 새 결과" 로 병합해야 한다.
    timeline_prompt = timeline_agent.llm.calls[0].prompt
    assert "다시 뽑은 후보" in timeline_prompt
    assert "처음 후보" not in timeline_prompt


def test_rerun_event_agent_reports_unknown_agent_name():
    llm = FakeLLM(
        [
            _plan([{"tool": "rerun_event_agent", "args": {"agent": "없는에이전트"}}]),
            _NOTHING_TO_FIX,
        ]
    )

    result = RepairAgent(llm=llm, max_iterations=2).generate(
        _request(),
        _draft(_event("체류", "09:00", "10:00")),
        event_agents={"location": StubEventAgent(AgentEventResult(), name="location")},
    )

    assert result.events[0].title == "체류"
    assert "없습니다" in llm.calls[1].prompt


# --- 프롬프트 구성 --------------------------------------------------------------


def test_prompt_carries_draft_sources_and_tools():
    llm = FakeLLM([_NOTHING_TO_FIX])

    RepairAgent(llm=llm, max_iterations=1).generate(
        _request(),
        _draft(_event("체류", "09:00", "10:00")),
        event_agents={"location": StubEventAgent(AgentEventResult(), name="location")},
    )

    prompt = llm.calls[0].prompt
    assert "[draft]" in prompt
    assert "rawId=s-1" in prompt  # 근거 원본 목록
    assert "update_event" in prompt  # 도구 카탈로그
    assert "location" in prompt  # 다시 돌릴 수 있는 Event Agent
    assert llm.calls[0].system is not None
