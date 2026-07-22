"""수집한 관측 이벤트를 Elasticsearch 용 task/event 문서로 조립한다.

외부 상관키는 ``taskId`` 하나만 사용한다. 각 이벤트의 Elasticsearch ``_id`` 는
export 시점에 ``taskId`` 와 ``sequence`` 로 파생하지만, 별도의 trace/span 식별자를
문서 계약에 노출하지 않는다. payload 는 Observer 에서 이미 마스킹·크기 제한된 값이다.
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
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """이벤트 버퍼를 ``(task 요약 1건, event 문서 N건)`` 으로 조립한다."""

    if not events:
        return None, []

    ordered = sorted(
        events,
        key=lambda event: event.sequence if event.sequence is not None else -1,
    )
    task_id = ordered[0].task_id
    event_documents = [
        _event_document(event, agent_version=agent_version) for event in ordered
    ]

    final = next(
        (event for event in reversed(ordered) if event.stage is ObservationStage.FINAL),
        None,
    )
    if final is None:
        status = "INCOMPLETE"
    elif final.event_type is ObservationEventType.FAILED:
        status = "FAILED"
    else:
        status = "SUCCESS"

    totals: dict[str, int] = {}
    llm_calls = 0
    for event in ordered:
        if (
            event.stage is ObservationStage.LLM
            and event.event_type is ObservationEventType.PROMPT
        ):
            llm_calls += 1
        if event.stage is not ObservationStage.LLM or event.event_type not in {
            ObservationEventType.RESPONSE,
            ObservationEventType.FAILED,
        }:
            continue
        for key, value in _token_usage(event).items():
            totals[key] = totals.get(key, 0) + value

    first = ordered[0]
    last = ordered[-1]
    task_document: dict[str, Any] = {
        "@timestamp": first.timestamp.isoformat(),
        "completedAt": last.timestamp.isoformat(),
        "schemaVersion": first.schema_version,
        "taskId": task_id,
        "status": status,
        "durationMs": max(
            (last.timestamp - first.timestamp).total_seconds() * 1000,
            0.0,
        ),
        "eventCount": len(event_documents),
        "droppedEventCount": dropped_event_count,
        "llmCallCount": llm_calls,
    }
    if totals:
        task_document["tokenUsage"] = totals
    if agent_version:
        task_document["agentVersion"] = agent_version
    return task_document, event_documents
