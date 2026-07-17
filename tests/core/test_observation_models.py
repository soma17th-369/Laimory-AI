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
        transactionId="tx-123",
        timestamp=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        stage=ObservationStage.LLM,
        eventType=ObservationEventType.COMPLETED,
        agent="location",
        provider="openai",
        model="gpt-5",
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
    assert record["transactionId"] == "tx-123"
    assert record["timestamp"] == "2026-07-17T12:00:00Z"
    assert record["stage"] == "LLM"
    assert record["eventType"] == "COMPLETED"
    assert record["durationMs"] == 12.5
    assert record["inputTokens"] == 10
    assert record["outputTokens"] == 20
    assert record["totalTokens"] == 30
    assert record["cachedTokens"] == 4
    assert record["reasoningTokens"] == 3
    assert record["toolTokens"] == 2


def test_observation_event_requires_transaction_id() -> None:
    with pytest.raises(ValidationError):
        ObservationEvent(
            transactionId="",
            stage=ObservationStage.REQUEST,
            eventType=ObservationEventType.STARTED,
        )


def test_repair_iteration_must_start_at_one() -> None:
    with pytest.raises(ValidationError):
        ObservationEvent(
            transactionId="tx-123",
            stage=ObservationStage.REPAIR_AGENT,
            eventType=ObservationEventType.PLAN,
            iteration=0,
        )
