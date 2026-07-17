"""관측 이벤트 출력 대상(sink) 구현."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Protocol, TextIO

from app.core.observability.models import ObservationEvent


class ObservationSink(Protocol):
    def write(self, event: ObservationEvent) -> None: ...


class NullObservationSink:
    def write(self, event: ObservationEvent) -> None:
        return None


class InMemoryObservationSink:
    """단위 테스트와 로컬 검사에서 사용하는 메모리 sink."""

    def __init__(self) -> None:
        self.events: list[ObservationEvent] = []
        self._lock = Lock()

    def write(self, event: ObservationEvent) -> None:
        with self._lock:
            self.events.append(event)


class JsonLinesObservationSink:
    """파일 경로나 열린 text stream에 이벤트를 JSONL로 기록한다."""

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
    """일부 sink 기록 실패를 모두 실행한 뒤 Observer에 전달한다."""

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
            except Exception as exc:  # noqa: BLE001 - 관측 실패는 나머지 sink를 막지 않는다.
                errors.append(exc)
        if errors:
            raise CompositeObservationError(errors)
