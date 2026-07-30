"""무상태 흐름의 단계별 관측 연결 검증.

- 실제 main agent 그래프를 태워 MAIN/EVENT/TIMELINE/REPAIR 단계가 taskId 로 이어져
  기록되는지(그리고 to_thread worker 까지 전파되는지) 본다.
- `process_timeline_task` 가 REQUEST/STORAGE/CALLBACK/FINAL 을 남기는지 본다.
"""

import asyncio
import json

from app.agents.main import run_main_agent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core.observability import (
    InMemoryObservationSink,
    Observer,
    observation_context,
)
from app.schemas import AgentEventResult, TaskStatus, TimelineDraft
from app.services import timeline_runner
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.pipeline import StubEventAgent, confirm_only_repair_agent
from tests.fixtures.requests import default_source_items, make_request, make_snapshot

_TASK_ID = "task-obs"
_DAILY_RECORD_ID = 42
_WINDOW_START = "2026-06-20T00:00:00+09:00"
_WINDOW_END = "2026-06-21T00:00:00+09:00"


def _stage_pairs(sink: InMemoryObservationSink) -> set[tuple[str, str]]:
    return {(e.stage.value, e.event_type.value) for e in sink.events}


def test_full_pipeline_emits_agent_stages_under_task_id() -> None:
    request = make_request()
    empty_draft_json = json.dumps(
        {"events": [], "questions": [], "warnings": []}, ensure_ascii=False
    )

    sink = InMemoryObservationSink()
    observer = Observer(sink)

    async def run() -> None:
        with observation_context("task-pipe", observer):
            await run_main_agent(
                request,
                event_agents=[StubEventAgent(AgentEventResult(), name="location")],
                timeline_agent=TimelineAgent(llm=FakeLLM([empty_draft_json])),
                repair_agent=confirm_only_repair_agent(),
            )

    asyncio.run(run())

    pairs = _stage_pairs(sink)
    for stage in ("MAIN_AGENT", "EVENT_AGENT", "TIMELINE_AGENT", "REPAIR_AGENT"):
        assert (stage, "STARTED") in pairs, stage
        assert (stage, "COMPLETED") in pairs, stage

    # 상관키는 taskId 하나뿐, 모든 이벤트가 동일해야 한다.
    assert {e.task_id for e in sink.events} == {"task-pipe"}
    # EVENT_AGENT 이벤트는 to_thread worker 에서 나왔는데도 agent 명이 살아 있다.
    event_agent_events = [e for e in sink.events if e.stage.value == "EVENT_AGENT"]
    assert event_agent_events
    assert all(e.agent == "location" for e in event_agent_events)
    # Observer 가 붙인 sequence 는 task 안에서 유일하다.
    seqs = [e.sequence for e in sink.events]
    assert len(seqs) == len(set(seqs))
    main_started = next(
        event
        for event in sink.events
        if event.stage.value == "MAIN_AGENT" and event.event_type.value == "STARTED"
    )
    assert main_started.payload["request"]["taskId"] == request.task_id
    event_completed = next(
        event
        for event in sink.events
        if event.stage.value == "EVENT_AGENT"
        and event.event_type.value == "COMPLETED"
    )
    assert "result" in event_completed.payload
    timeline_completed = next(
        event
        for event in sink.events
        if event.stage.value == "TIMELINE_AGENT"
        and event.event_type.value == "COMPLETED"
    )
    assert "timeline" in timeline_completed.payload


def _seeded_client(**overrides) -> FakeAppServerClient:
    defaults = dict(
        snapshot=make_snapshot(task_id=_TASK_ID, source_items=default_source_items())
    )
    defaults.update(overrides)
    return FakeAppServerClient(**defaults)


def _capture_flush(monkeypatch) -> dict:
    captured: dict = {}
    sink = InMemoryObservationSink()
    observer = Observer(sink)

    monkeypatch.setattr(
        timeline_runner,
        "build_task_observer",
        lambda: (observer, sink),
    )

    async def fake_flush(buffer, *, task_id):
        captured["events"] = list(buffer.events)
        captured["task_id"] = task_id

    monkeypatch.setattr(timeline_runner, "flush_task_observations", fake_flush)
    return captured


def test_runner_emits_request_storage_callback_final(monkeypatch) -> None:
    async def fake_main_agent(request):
        return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    captured = _capture_flush(monkeypatch)

    status = asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID,
            _seeded_client(),
            _DAILY_RECORD_ID,
            _WINDOW_START,
            _WINDOW_END,
            "task-token",
        )
    )

    assert status is TaskStatus.SUCCESS
    events = captured["events"]
    pairs = {(e.stage.value, e.event_type.value) for e in events}
    assert ("REQUEST", "STARTED") in pairs
    assert ("REQUEST", "COMPLETED") in pairs
    assert ("STORAGE", "STARTED") in pairs
    assert ("STORAGE", "COMPLETED") in pairs
    assert ("CALLBACK", "STARTED") in pairs
    assert ("CALLBACK", "COMPLETED") in pairs
    assert ("FINAL", "COMPLETED") in pairs
    assert {e.task_id for e in events} == {_TASK_ID}
    request_completed = next(
        event
        for event in events
        if event.stage.value == "REQUEST" and event.event_type.value == "COMPLETED"
    )
    assert request_completed.payload["request"]["taskId"] == _TASK_ID
    serialized_payload = json.dumps(request_completed.payload, ensure_ascii=False)
    assert '"stays"' in serialized_payload
    assert '"rawId"' in serialized_payload
    serialized_events = json.dumps(
        [event.model_dump(mode="json") for event in events],
        ensure_ascii=False,
    )
    assert "task-token" not in serialized_events
    # sequence 는 발급 순서대로 단조 증가한다.
    assert [e.sequence for e in events] == sorted(e.sequence for e in events)


def test_runner_missing_input_emits_request_failed_and_final_failed(
    monkeypatch,
) -> None:
    captured = _capture_flush(monkeypatch)

    status = asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID,
            FakeAppServerClient(snapshot=None),  # 입력 조회 404
            _DAILY_RECORD_ID,
            _WINDOW_START,
            _WINDOW_END,
            "task-token",
        )
    )

    assert status is TaskStatus.FAILED
    pairs = {(e.stage.value, e.event_type.value) for e in captured["events"]}
    assert ("REQUEST", "STARTED") in pairs
    assert ("REQUEST", "FAILED") in pairs
    assert ("FINAL", "FAILED") in pairs
    # 404 는 콜백도 거절되므로 CALLBACK 단계 자체가 없다.
    assert not any(stage == "CALLBACK" for stage, _ in pairs)


def test_runner_timeout_emits_main_and_final_failed(monkeypatch) -> None:
    async def slow_main_agent(request):
        await asyncio.sleep(0.05)
        return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    monkeypatch.setattr(timeline_runner, "run_main_agent", slow_main_agent)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.001)
    captured = _capture_flush(monkeypatch)

    status = asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID,
            _seeded_client(),
            _DAILY_RECORD_ID,
            _WINDOW_START,
            _WINDOW_END,
            "task-token",
        )
    )

    assert status is TaskStatus.FAILED
    failed = [
        event
        for event in captured["events"]
        if event.event_type.value == "FAILED"
    ]
    assert any(event.stage.value == "MAIN_AGENT" for event in failed)
    assert any(event.stage.value == "FINAL" for event in failed)
