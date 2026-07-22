"""이벤트 버퍼 → task/event Elasticsearch 문서 조립 검증."""

from datetime import datetime, timedelta, timezone

from app.core.observability import ObservationEvent, ObservationEventType, ObservationStage
from app.core.observability.documents import build_documents

_BASE = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)


def _ev(seq, stage, event_type, **kw) -> ObservationEvent:
    return ObservationEvent(
        task_id="t1",
        sequence=seq,
        timestamp=_BASE + timedelta(seconds=seq),
        stage=stage,
        event_type=event_type,
        **kw,
    )


def _run_events() -> list[ObservationEvent]:
    stage = ObservationStage
    event = ObservationEventType
    return [
        _ev(0, stage.REQUEST, event.STARTED, payload={"content": {"input": "safe"}}),
        _ev(1, stage.LLM, event.PROMPT, provider="bedrock", model="nova"),
        _ev(
            2,
            stage.LLM,
            event.RESPONSE,
            provider="bedrock",
            model="nova",
            duration_ms=100.0,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            payload={"content": {"response": "ok"}},
        ),
        _ev(3, stage.FINAL, event.COMPLETED, payload={"content": {"status": "SUCCESS"}}),
    ]


def test_build_documents_empty() -> None:
    task, events = build_documents([])
    assert task is None and events == []


def test_task_summary_uses_task_id_only() -> None:
    task, events = build_documents(
        _run_events(),
        agent_version="v7",
        dropped_event_count=2,
    )

    assert task["taskId"] == "t1"
    assert task["status"] == "SUCCESS"
    assert task["llmCallCount"] == 1
    assert task["tokenUsage"] == {"input": 10, "output": 5, "total": 15}
    assert task["agentVersion"] == "v7"
    assert task["durationMs"] == 3000.0
    assert task["eventCount"] == len(events)
    assert task["droppedEventCount"] == 2
    assert "traceId" not in task and "spanId" not in task


def test_event_documents_keep_safe_payload_and_sequence() -> None:
    _, events = build_documents(_run_events())

    assert all(event["taskId"] == "t1" for event in events)
    assert [event["sequence"] for event in events] == [0, 1, 2, 3]
    assert all("traceId" not in event and "spanId" not in event for event in events)

    prompt = next(event for event in events if event["eventType"] == "PROMPT")
    response = next(event for event in events if event["eventType"] == "RESPONSE")
    assert prompt["status"] == "STARTED"
    assert response["payload"] == {"content": {"response": "ok"}}
    assert response["durationMs"] == 100.0
    assert response["tokenUsage"] == {"input": 10, "output": 5, "total": 15}
    assert response["modelProvider"] == "bedrock"
    assert response["modelId"] == "nova"


def test_failed_final_marks_task_failed_and_keeps_error() -> None:
    events = [
        _ev(0, ObservationStage.REQUEST, ObservationEventType.STARTED),
        _ev(
            1,
            ObservationStage.FINAL,
            ObservationEventType.FAILED,
            payload={"content": {"error": "timeout"}},
        ),
    ]
    task, documents = build_documents(events)

    assert task["status"] == "FAILED"
    assert documents[-1]["status"] == "FAILED"
    assert documents[-1]["payload"]["content"]["error"] == "timeout"
