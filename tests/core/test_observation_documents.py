"""이벤트 버퍼 → 단일 Elasticsearch event 문서 조립 검증."""

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
        _ev(0, stage.REQUEST, event.STARTED, payload={"inputItemCount": 3}),
        _ev(
            1,
            stage.LLM,
            event.PROMPT,
            provider="bedrock",
            model="nova",
            payload={"promptMetadata": {"contentCaptured": False, "byteLength": 10}},
        ),
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
            payload={"responseMetadata": {"contentCaptured": False, "byteLength": 5}},
        ),
        _ev(3, stage.FINAL, event.COMPLETED, payload={"status": "SUCCESS"}),
    ]


def test_build_documents_empty() -> None:
    assert build_documents([]) == []


def test_documents_use_task_id_only_and_keep_event_metadata() -> None:
    documents = build_documents(
        _run_events(),
        agent_version="v7",
        dropped_event_count=2,
    )

    assert [document["sequence"] for document in documents] == [0, 1, 2, 3]
    assert all(document["taskId"] == "t1" for document in documents)
    assert all("traceId" not in document and "spanId" not in document for document in documents)
    assert all(document["agentVersion"] == "v7" for document in documents)

    response = next(document for document in documents if document["eventType"] == "RESPONSE")
    assert response["durationMs"] == 100.0
    assert response["tokenUsage"] == {"input": 10, "output": 5, "total": 15}
    assert response["modelProvider"] == "bedrock"
    assert response["modelId"] == "nova"

    final = documents[-1]
    assert final["status"] == "SUCCESS"
    assert final["taskDurationMs"] == 3000.0
    assert final["droppedEventCount"] == 2
    assert final["eventCount"] == 4


def test_failed_final_keeps_failure_metadata_without_error_body() -> None:
    documents = build_documents(
        [
            _ev(0, ObservationStage.REQUEST, ObservationEventType.STARTED),
            _ev(
                1,
                ObservationStage.FINAL,
                ObservationEventType.FAILED,
                payload={"failureReason": "processing_failed"},
            ),
        ]
    )

    assert documents[-1]["status"] == "FAILED"
    assert documents[-1]["payload"] == {"failureReason": "processing_failed"}
