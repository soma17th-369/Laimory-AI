"""관측 이벤트 계약 검증."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core.observability import (
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
)


def test_observation_event_serializes_with_external_field_names() -> None:
    event = ObservationEvent(
        taskId="task-123",
        sequence=7,
        timestamp=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        stage=ObservationStage.LLM,
        eventType=ObservationEventType.COMPLETED,
        agent="location",
        provider="openai",
        model="gpt-5",
        agentVersion="2026.07.23",
        providerVersion="1.50.0",
        durationMs=12.5,
        inputTokens=10,
        outputTokens=20,
        totalTokens=30,
        cachedTokens=4,
        reasoningTokens=3,
        toolTokens=2,
        payload={"result": "ok"},
    )

    record = event.to_record()

    assert record["schemaVersion"] == "1"
    assert record["taskId"] == "task-123"
    assert record["sequence"] == 7
    assert record["timestamp"] == "2026-07-23T12:00:00Z"
    assert record["stage"] == "LLM"
    assert record["eventType"] == "COMPLETED"
    assert record["agentVersion"] == "2026.07.23"
    assert record["providerVersion"] == "1.50.0"
    assert record["durationMs"] == 12.5
    assert record["inputTokens"] == 10
    assert record["outputTokens"] == 20
    assert record["totalTokens"] == 30
    assert record["cachedTokens"] == 4
    assert record["reasoningTokens"] == 3
    assert record["toolTokens"] == 2
    # 상관키는 taskId 하나뿐 — 별도 실행 식별자를 만들지 않는다.
    assert "transactionId" not in record


def test_unset_optional_fields_are_omitted_from_record() -> None:
    event = ObservationEvent(
        taskId="task-123",
        stage=ObservationStage.REQUEST,
        eventType=ObservationEventType.STARTED,
    )

    record = event.to_record()

    # sequence 는 Observer 가 emit 시점에 붙이므로, 직접 만든 이벤트에는 없다.
    assert "sequence" not in record
    assert "provider" not in record
    assert "inputTokens" not in record


def test_observation_event_requires_task_id() -> None:
    with pytest.raises(ValidationError):
        ObservationEvent(
            taskId="",
            stage=ObservationStage.REQUEST,
            eventType=ObservationEventType.STARTED,
        )


def test_repair_iteration_must_start_at_one() -> None:
    with pytest.raises(ValidationError):
        ObservationEvent(
            taskId="task-123",
            stage=ObservationStage.REPAIR_AGENT,
            eventType=ObservationEventType.PLAN,
            iteration=0,
        )
