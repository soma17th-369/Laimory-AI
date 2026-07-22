"""관측 이벤트 출력 대상(sink) 구현.

여기의 sink 는 특정 관측 제품에 의존하지 않는다. Elasticsearch 전송 sink 는
``app/core/observability/elasticsearch.py`` 에 따로 둔다.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Protocol, TextIO

from app.core.observability.models import ObservationEvent


class ObservationSink(Protocol):
    def write(self, event: ObservationEvent) -> None: ...


class NullObservationSink:
    """아무 데도 쓰지 않는 기본 sink (운영 기본값)."""

    def write(self, event: ObservationEvent) -> None:
        return None


class InMemoryObservationSink:
    """요청 단위 수집 버퍼. 이벤트 수 상한으로 메모리 사용을 제한한다."""

    def __init__(self, *, max_events: int = 1000) -> None:
        if max_events <= 0:
            raise ValueError("max_events 는 1 이상이어야 합니다.")
        self.events: list[ObservationEvent] = []
        self.dropped_count = 0
        self._max_events = max_events
        self._lock = Lock()

    def write(self, event: ObservationEvent) -> None:
        with self._lock:
            if len(self.events) < self._max_events:
                self.events.append(event)
                return

            self.dropped_count += 1
            # 버퍼가 꽉 차도 최종 상태와 실패 원인은 최대한 보존한다.
            if event.stage.value == "FINAL" or event.event_type.value == "FAILED":
                self.events[-1] = event


class JsonLinesObservationSink:
    """파일 경로나 열린 text stream 에 이벤트를 JSONL 로 기록한다."""

    def __init__(self, destination: str | Path | TextIO) -> None:
        self._destination = destination
        self._lock = Lock()

    def write(self, event: ObservationEvent) -> None:
        line = json.dumps(event.to_record(), ensure_ascii=False) + "\n"
        with self._lock:
            if hasattr(self._destination, "write"):
                self._destination.write(line)
                if hasattr(self._destination, "flush"):
                    self._destination.flush()
                return

            path = Path(self._destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)


class CompositeObservationError(RuntimeError):
    """일부 sink 기록 실패를 모두 실행한 뒤 Observer 에 전달한다."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__(f"관측 sink {len(errors)}개 기록 실패")
        self.errors = errors


class CompositeObservationSink:
    def __init__(self, sinks: list[ObservationSink]) -> None:
        self._sinks = list(sinks)

    def write(self, event: ObservationEvent) -> None:
        errors: list[Exception] = []
        for sink in self._sinks:
            try:
                sink.write(event)
            except Exception as exc:  # noqa: BLE001 - 관측 실패는 나머지 sink 를 막지 않는다.
                errors.append(exc)
        if errors:
            raise CompositeObservationError(errors)
