"""Repair Agent 검증.

보는 것은 네 가지다.

    1. 코드 확정은 LLM 이 무엇을 하든 항상 지나간다(정렬·clientEventId 재부여).
    2. LLM 이 낸 계획이 도구로 실행되어 draft 에 반영된다.
    3. 반복은 `done` 이나 상한에서 멈춘다.
    4. 실패해도 draft 를 잃지 않는다(직전 확정 draft + warning).

LLM 은 `FakeLLM` 으로 대신하고, 실제 호출은 하지 않는다.
"""

import json

from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.agents.repair import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core import langfuse_tracing
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
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


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
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id(raw_id),
            )
        ],
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
    assert all("LLM 장애" not in warning.message for warning in result.warnings)


def test_langfuse_repair_iteration_nests_plan_tools_and_draft(
    monkeypatch,
) -> None:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-repair-hierarchy",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:1",
        span_exporter=exporter,
    )
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(
        langfuse_tracing.settings,
        "langfuse_content_capture",
        "SANITIZED",
    )
    plan = _plan(
        [
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-001",
                    "fields": {"title": "카페에서 쉬었다"},
                },
            }
        ],
        done=True,
    )

    with langfuse_tracing.trace_observation(
        "repair-agent",
        as_type="agent",
    ):
        RepairAgent(llm=FakeLLM([plan]), max_iterations=2).generate(
            _request(),
            _draft(_event("체류", "09:00", "10:00")),
        )
    client.flush()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    expected = {
        "repair-agent",
        "confirm-timeline-draft",
        "analyze-repair-iteration",
        "execute-repair-plan",
        "confirm-repair-iteration",
        "execute-update-event",
    }
    assert expected <= spans.keys()

    repair = spans["repair-agent"]
    for child_name in (
        "confirm-timeline-draft",
        "analyze-repair-iteration",
        "execute-repair-plan",
        "confirm-repair-iteration",
    ):
        assert spans[child_name].parent.span_id == repair.context.span_id
    assert (
        spans["execute-update-event"].parent.span_id
        == spans["execute-repair-plan"].context.span_id
    )

    analyze_input = json.loads(
        spans["analyze-repair-iteration"].attributes[
            "langfuse.observation.input"
        ]
    )
    assert "[draft]" in analyze_input["prompt"]
    assert analyze_input["system"]
    analyze_output = json.loads(
        spans["analyze-repair-iteration"].attributes[
            "langfuse.observation.output"
        ]
    )
    assert analyze_output["plan"]["toolCalls"][0]["tool"] == "update_event"

    tool_input = json.loads(
        spans["execute-update-event"].attributes[
            "langfuse.observation.input"
        ]
    )
    tool_output = json.loads(
        spans["execute-update-event"].attributes[
            "langfuse.observation.output"
        ]
    )
    assert tool_input["call"]["args"]["fields"]["title"] == "카페에서 쉬었다"
    assert tool_output["result"]["ok"] is True
    assert tool_output["timeline"]["events"][0]["title"] == "카페에서 쉬었다"
    confirmation_output = json.loads(
        spans["confirm-repair-iteration"].attributes[
            "langfuse.observation.output"
        ]
    )
    assert confirmation_output["timeline"]["events"][0]["title"] == (
        "카페에서 쉬었다"
    )


def test_langfuse_repair_loop_repeats_analyze_execute_confirm(
    monkeypatch,
) -> None:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-repair-loop",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:1",
        span_exporter=exporter,
    )
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(
        langfuse_tracing.settings,
        "langfuse_content_capture",
        "SANITIZED",
    )
    first_plan = _plan(
        [
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-001",
                    "fields": {"title": "첫 번째 수정"},
                },
            }
        ],
        done=False,
    )
    final_plan = _plan([], done=True)

    with langfuse_tracing.trace_observation(
        "repair-agent",
        as_type="agent",
    ):
        RepairAgent(
            llm=FakeLLM([first_plan, final_plan]),
            max_iterations=3,
        ).generate(
            _request(),
            _draft(_event("체류", "09:00", "10:00")),
        )
    client.flush()

    spans = exporter.get_finished_spans()
    assert sum(span.name == "analyze-repair-iteration" for span in spans) == 2
    assert sum(span.name == "execute-repair-plan" for span in spans) == 1
    assert sum(span.name == "confirm-repair-iteration" for span in spans) == 1


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
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id(raw_id),
            )
        ],
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
                    "sourceRefs": [
                        {
                            "sourceType": "STAY",
                            "rawId": fixture_raw_id("s-1"),
                        }
                    ],
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
    assert f"rawId={fixture_raw_id('s-1')}" in prompt  # 근거 원본 목록
    assert "update_event" in prompt  # 도구 카탈로그
    assert "location" in prompt  # 다시 돌릴 수 있는 Event Agent
    assert llm.calls[0].system is not None


# --- 확정본 발행 (이슈 #76) ----------------------------------------------------


def test_every_confirm_publishes_a_draft():
    """확정할 때마다 발행한다. 초기 확정 1회 + 반복마다 1회.

    호출자는 제한 시간이 끝나 이 실행이 취소돼도 이 발행본으로 저장할 수 있어야 한다.
    """

    published: list[TimelineDraft] = []
    never_done = _plan(
        [
            {
                "tool": "update_event",
                "args": {"clientEventId": "event-001", "fields": {"title": "고침"}},
            }
        ]
    )

    RepairAgent(llm=FakeLLM([never_done]), max_iterations=2).generate(
        _request(),
        _draft(_event("체류", "09:00", "10:00")),
        on_confirm=published.append,
    )

    assert len(published) == 3  # 초기 확정 + 반복 2회
    assert published[-1].events[0].title == "고침"


def test_published_draft_is_already_confirmed():
    """발행본은 정렬·clientEventId 재부여가 끝난 상태여야 한다.

    중간 상태를 저장하면 질문의 `relatedEventIds` 가 가리키는 곳이 어긋난다.
    """

    published: list[TimelineDraft] = []
    draft = _draft(
        _event("오후", "15:00", "16:00", raw_id="s-2", client_event_id="event-001"),
        _event("오전", "09:00", "10:00", raw_id="s-1", client_event_id="event-002"),
    )

    RepairAgent(llm=FakeLLM([_NOTHING_TO_FIX]), max_iterations=2).generate(
        _request(), draft, on_confirm=published.append
    )

    first = published[0]
    assert [event.title for event in first.events] == ["오전", "오후"]
    assert [event.client_event_id for event in first.events] == [
        "event-001",
        "event-002",
    ]


def test_published_draft_is_a_copy_not_a_live_reference():
    """발행 뒤의 변경이 이미 발행한 값에 닿으면 안 된다.

    `asyncio.wait_for` 는 스레드를 끊지 못한다. 호출자가 취소한 뒤에도 이 Agent 는
    계속 돌며 draft 를 고치므로, 참조를 넘기면 저장 직전에 값이 바뀐다.
    """

    published: list[TimelineDraft] = []
    rename = _plan(
        [
            {
                "tool": "update_event",
                "args": {"clientEventId": "event-001", "fields": {"title": "나중 제목"}},
            }
        ]
    )

    result = RepairAgent(llm=FakeLLM([rename]), max_iterations=1).generate(
        _request(),
        _draft(_event("처음 제목", "09:00", "10:00")),
        on_confirm=published.append,
    )

    # 초기 확정본은 수정 전 제목을 그대로 들고 있어야 한다.
    assert published[0].events[0].title == "처음 제목"
    assert result.events[0].title == "나중 제목"


def test_on_confirm_is_optional():
    """주지 않으면 지금과 똑같이 동작한다."""

    result = RepairAgent(llm=FakeLLM([_NOTHING_TO_FIX]), max_iterations=1).generate(
        _request(), _draft(_event("체류", "09:00", "10:00"))
    )

    assert result.events[0].title == "체류"
