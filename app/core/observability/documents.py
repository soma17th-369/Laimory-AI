"""수집한 관측 이벤트를 Elasticsearch event 문서로 조립한다.

외부 상관키는 ``taskId`` 하나만 사용한다. Elasticsearch ``_id``는 export 시점에
``taskId``와 ``sequence``로 파생하고 별도 식별자 필드는 만들지 않는다. 별도의 task
요약 문서도 만들지 않으며, 종료 상태와 task 전체 처리시간은 FINAL 이벤트에 담는다.
"""

from __future__ import annotations

from typing import Any

from app.core.observability.models import (
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
)

_TOKEN_FIELDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("total", "total_tokens"),
    ("cached", "cached_tokens"),
    ("reasoning", "reasoning_tokens"),
    ("tool", "tool_tokens"),
)


def _token_usage(event: ObservationEvent) -> dict[str, int]:
    usage: dict[str, int] = {}
    for key, attr in _TOKEN_FIELDS:
        value = getattr(event, attr)
        if value is not None:
            usage[key] = value
    return usage


def _event_status(event: ObservationEvent) -> str:
    if event.event_type is ObservationEventType.FAILED:
        return "FAILED"
    if event.event_type in {
        ObservationEventType.STARTED,
        ObservationEventType.PROMPT,
    }:
        return "STARTED"
    return "SUCCESS"


def _event_document(
    event: ObservationEvent,
    *,
    agent_version: str,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "@timestamp": event.timestamp.isoformat(),
        "schemaVersion": event.schema_version,
        "taskId": event.task_id,
        "sequence": event.sequence,
        "stage": event.stage.value,
        "eventType": event.event_type.value,
        "status": _event_status(event),
        "payload": event.payload,
    }
    if event.agent is not None:
        document["agentName"] = event.agent
    if event.iteration is not None:
        document["iteration"] = event.iteration
    if event.provider is not None:
        document["modelProvider"] = event.provider
    if event.model is not None:
        document["modelId"] = event.model
    if event.provider_version is not None:
        document["providerVersion"] = event.provider_version
    effective_agent_version = event.agent_version or agent_version
    if effective_agent_version:
        document["agentVersion"] = effective_agent_version
    if event.duration_ms is not None:
        document["durationMs"] = event.duration_ms
    usage = _token_usage(event)
    if usage:
        document["tokenUsage"] = usage
    return document


def build_documents(
    events: list[ObservationEvent],
    *,
    agent_version: str = "",
    dropped_event_count: int = 0,
) -> list[dict[str, Any]]:
    """이벤트 버퍼를 Elasticsearch event 문서 목록으로 조립한다."""

    if not events:
        return []

    ordered = sorted(
        events,
        key=lambda event: event.sequence if event.sequence is not None else -1,
    )
    documents = [
        _event_document(event, agent_version=agent_version) for event in ordered
    ]

    final_index = next(
        (
            index
            for index in range(len(ordered) - 1, -1, -1)
            if ordered[index].stage is ObservationStage.FINAL
        ),
        None,
    )
    if final_index is not None:
        final = ordered[final_index]
        documents[final_index]["taskDurationMs"] = max(
            (final.timestamp - ordered[0].timestamp).total_seconds() * 1000,
            0.0,
        )
        documents[final_index]["droppedEventCount"] = dropped_event_count
        documents[final_index]["eventCount"] = len(documents)

    return documents
