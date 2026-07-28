"""관측 sink 기록, sequence 부여, 조합·실패 격리 검증."""

import io
import json

from app.core.observability import (
    CompositeObservationSink,
    ContentCapture,
    InMemoryObservationSink,
    JsonLinesObservationSink,
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
    Observer,
    REDACTED,
)


def _event() -> ObservationEvent:
    return ObservationEvent(
        taskId="task-123",
        stage=ObservationStage.EVENT_AGENT,
        eventType=ObservationEventType.PROMPT,
        agent="location",
        payload={"prompt": "user@example.com", "apiKey": "secret"},
    )


class _FailingSink:
    def write(self, event: ObservationEvent) -> None:
        raise RuntimeError("의도된 관측 장애")


def test_observer_sanitizes_content_before_writing_to_sink() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink)

    assert observer.emit(_event()) is True

    stored = sink.events[0]
    assert stored.payload["prompt"] == REDACTED
    assert stored.payload["apiKey"] == REDACTED
    assert "user@example.com" not in str(stored.payload)
    assert observer.stats().attempted == 1
    assert observer.stats().succeeded == 1
    assert observer.stats().failed == 0


def test_observer_keeps_sanitized_content_by_default() -> None:
    sink = InMemoryObservationSink()

    Observer(sink).emit(_event())

    assert sink.events[0].payload["prompt"] == REDACTED
    assert "user@example.com" not in str(sink.events[0].payload)


def test_observer_none_policy_hides_content() -> None:
    sink = InMemoryObservationSink()

    Observer(sink, content_capture=ContentCapture.NONE).emit(_event())

    assert sink.events[0].payload["prompt"]["contentCaptured"] is False
    assert "user@example.com" not in str(sink.events[0].payload)


def test_observer_assigns_monotonic_sequence_per_task() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink)

    for _ in range(3):
        observer.emit(_event())

    assert [e.sequence for e in sink.events] == [0, 1, 2]
    assert all(e.task_id == "task-123" for e in sink.events)


def test_memory_sink_caps_events_but_keeps_final() -> None:
    sink = InMemoryObservationSink(max_events=2)
    observer = Observer(sink)

    observer.emit(_event())
    observer.emit(_event())
    observer.emit(_event())
    observer.emit(
        _event().model_copy(
            update={
                "stage": ObservationStage.FINAL,
                "event_type": ObservationEventType.FAILED,
            }
        )
    )

    assert len(sink.events) == 2
    assert sink.dropped_count == 2
    assert sink.events[-1].stage is ObservationStage.FINAL


def test_composite_continues_other_sinks_and_observer_absorbs_failure() -> None:
    healthy = InMemoryObservationSink()
    observer = Observer(
        CompositeObservationSink([_FailingSink(), healthy]),
    )

    assert observer.emit(_event()) is False

    assert len(healthy.events) == 1
    assert observer.stats().attempted == 1
    assert observer.stats().succeeded == 0
    assert observer.stats().failed == 1


def test_json_lines_sink_writes_one_valid_json_object_per_event() -> None:
    stream = io.StringIO()
    observer = Observer(
        JsonLinesObservationSink(stream),
    )

    observer.emit(_event())
    observer.emit(
        _event().model_copy(update={"event_type": ObservationEventType.RESPONSE})
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["eventType"] == "PROMPT"
    assert second["eventType"] == "RESPONSE"
    assert first["taskId"] == "task-123"
    # Observer 가 붙인 task 단위 단조 sequence 로 순서를 재구성한다.
    assert first["sequence"] == 0
    assert second["sequence"] == 1
