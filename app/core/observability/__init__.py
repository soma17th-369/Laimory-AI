"""Timeline 관측 계약과 제품 독립 기록 인터페이스.

상관키는 ``taskId`` 하나이며, 한 task 안의 이벤트 순서는 Observer 가 붙이는
단조 증가 ``sequence`` 로 재구성한다. Elasticsearch 전송 sink 는
``app/core/observability/elasticsearch.py`` 에 따로 둔다(코어는 특정 제품에
의존하지 않는다).
"""

from app.core.observability.models import (
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
)
from app.core.observability.context import (
    ObservationContext,
    current_observation_context,
    emit_observation,
    observation_context,
    observation_scope,
)
from app.core.observability.observer import ObservationStats, Observer
from app.core.observability.redaction import (
    REDACTED,
    capture_payload,
    redact_value,
    summarize_content,
)
from app.core.observability.sinks import (
    CompositeObservationError,
    CompositeObservationSink,
    InMemoryObservationSink,
    JsonLinesObservationSink,
    NullObservationSink,
    ObservationSink,
)

__all__ = [
    "CompositeObservationError",
    "CompositeObservationSink",
    "InMemoryObservationSink",
    "JsonLinesObservationSink",
    "NullObservationSink",
    "ObservationEvent",
    "ObservationEventType",
    "ObservationSink",
    "ObservationStage",
    "ObservationStats",
    "ObservationContext",
    "Observer",
    "REDACTED",
    "capture_payload",
    "current_observation_context",
    "emit_observation",
    "observation_context",
    "observation_scope",
    "redact_value",
    "summarize_content",
]
