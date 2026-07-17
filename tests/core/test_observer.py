"""관측 sink 기록, 조합 및 실패 격리 검증."""

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
        transactionId="tx-123",
        stage=ObservationStage.EVENT_AGENT,
        eventType=ObservationEventType.PROMPT,
        agent="location",
        payload={"prompt": "user@example.com", "apiKey": "secret"},
    )


class _FailingSink:
    def write(self, event: ObservationEvent) -> None:
        raise RuntimeError("의도된 관측 장애")


def test_observer_sanitizes_before_writing_to_sink() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)

    assert observer.emit(_event()) is True

    stored = sink.events[0]
    assert stored.payload == {"prompt": REDACTED, "apiKey": REDACTED}
    assert observer.stats().attempted == 1
    assert observer.stats().succeeded == 1
    assert observer.stats().failed == 0


def test_observer_hides_content_by_default() -> None:
    sink = InMemoryObservationSink()

    Observer(sink).emit(_event())

    assert sink.events[0].payload["contentCaptured"] is False
    assert "user@example.com" not in str(sink.events[0].payload)


def test_composite_continues_other_sinks_and_observer_absorbs_failure() -> None:
    healthy = InMemoryObservationSink()
    observer = Observer(
        CompositeObservationSink([_FailingSink(), healthy]),
        content_capture=ContentCapture.SANITIZED,
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
        content_capture=ContentCapture.SANITIZED,
    )

    observer.emit(_event())
    observer.emit(
        _event().model_copy(update={"event_type": ObservationEventType.RESPONSE})
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["eventType"] == "PROMPT"
    assert json.loads(lines[1])["eventType"] == "RESPONSE"
    assert json.loads(lines[0])["transactionId"] == "tx-123"
