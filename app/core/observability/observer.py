"""마스킹·sequence 부여·sink 실패 격리를 담당하는 관측 진입점."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from threading import Lock

from app.core.logging import get_logger
from app.core.observability.models import ContentCapture, ObservationEvent
from app.core.observability.redaction import capture_payload
from app.core.observability.sinks import NullObservationSink, ObservationSink

logger = get_logger(__name__)


@dataclass(frozen=True)
class ObservationStats:
    attempted: int
    succeeded: int
    failed: int


class Observer:
    """이벤트를 안전하게 정리해 sink 에 전달하고 실패를 호출자와 격리한다.

    요청(task)마다 하나씩 만들어야 한다. Observer 가 emit 순서대로 ``sequence`` 를
    붙이므로, 전역 싱글턴을 공유하면 서로 다른 task 의 로그와 sequence 가 섞인다.
    ``sequence`` 발급은 thread-safe 하며, 병렬 Event Agent 가 worker thread 에서
    동시에 emit 해도 값이 겹치지 않는다(실패한 emit 이 번호를 소비하면 빈 번호가
    생길 수 있으나, 중복은 없고 순서는 보존된다).
    """

    def __init__(
        self,
        sink: ObservationSink | None = None,
        *,
        content_capture: ContentCapture = ContentCapture.NONE,
        max_payload_bytes: int = 16 * 1024,
    ) -> None:
        self._sink = sink or NullObservationSink()
        self._content_capture = content_capture
        self._max_payload_bytes = max_payload_bytes
        self._sequence = count()
        self._sequence_lock = Lock()
        self._attempted = 0
        self._succeeded = 0
        self._failed = 0
        self._lock = Lock()

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            return next(self._sequence)

    def emit(self, event: ObservationEvent) -> bool:
        """관측 성공 여부를 반환하되 어떤 실패도 Timeline 흐름으로 전파하지 않는다."""

        with self._lock:
            self._attempted += 1
        try:
            safe_event = event.model_copy(
                update={
                    "sequence": self._next_sequence(),
                    "payload": capture_payload(
                        event.payload,
                        self._content_capture,
                        max_bytes=self._max_payload_bytes,
                    ),
                }
            )
            self._sink.write(safe_event)
        except Exception as exc:  # noqa: BLE001 - 관측은 주 처리의 실패 원인이 될 수 없다.
            with self._lock:
                self._failed += 1
            logger.warning(
                "관측 이벤트 기록 실패: taskId=%s, stage=%s, eventType=%s, error=%s",
                event.task_id,
                event.stage,
                event.event_type,
                exc,
            )
            return False

        with self._lock:
            self._succeeded += 1
        return True

    def stats(self) -> ObservationStats:
        with self._lock:
            return ObservationStats(
                attempted=self._attempted,
                succeeded=self._succeeded,
                failed=self._failed,
            )
