"""마스킹과 sink 실패 격리를 담당하는 관측 진입점."""

from __future__ import annotations

from dataclasses import dataclass
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
    """이벤트를 안전하게 정리해 sink에 전달하고 실패를 호출자와 격리한다."""

    def __init__(
        self,
        sink: ObservationSink | None = None,
        *,
        content_capture: ContentCapture = ContentCapture.NONE,
    ) -> None:
        self._sink = sink or NullObservationSink()
        self._content_capture = content_capture
        self._attempted = 0
        self._succeeded = 0
        self._failed = 0
        self._lock = Lock()

    def emit(self, event: ObservationEvent) -> bool:
        """관측 성공 여부를 반환하되 어떤 실패도 Timeline 흐름으로 전파하지 않는다."""

        with self._lock:
            self._attempted += 1
        try:
            safe_event = event.model_copy(
                update={
                    "payload": capture_payload(event.payload, self._content_capture)
                }
            )
            self._sink.write(safe_event)
        except Exception as exc:  # noqa: BLE001 - 관측은 주 처리의 실패 원인이 될 수 없다.
            with self._lock:
                self._failed += 1
            logger.warning(
                "관측 이벤트 기록 실패: transactionId=%s, stage=%s, eventType=%s, error=%s",
                event.transaction_id,
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
