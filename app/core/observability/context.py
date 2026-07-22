"""비동기·스레드 실행 사이에서 관측 상관관계를 전파하는 컨텍스트.

상관키는 ``taskId`` 하나다. ``process_timeline_task`` 가 task 별 Observer 와 함께
최상위 컨텍스트를 열면, ``asyncio.to_thread`` 가 contextvars 를 복사하므로 Event/
Timeline/Repair Agent worker thread 와 그 안의 LLM 호출까지 같은 taskId 컨텍스트가
따라간다. 컨텍스트가 없으면(스크립트·단위 테스트 등) emit 은 조용히 건너뛴다.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from dataclasses import dataclass
from typing import Any, Iterator

from app.core.observability.models import (
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
)
from app.core.observability.observer import Observer


@dataclass(frozen=True)
class ObservationContext:
    task_id: str
    observer: Observer
    stage: ObservationStage = ObservationStage.REQUEST
    agent: str | None = None
    iteration: int | None = None


_CURRENT: ContextVar[ObservationContext | None] = ContextVar(
    "timeline_observation_context",
    default=None,
)


def current_observation_context() -> ObservationContext | None:
    return _CURRENT.get()


@contextmanager
def observation_context(
    task_id: str,
    observer: Observer,
) -> Iterator[ObservationContext]:
    """Timeline 처리 전체가 공유할 최상위 관측 컨텍스트를 연다."""

    context = ObservationContext(task_id=task_id, observer=observer)
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


@contextmanager
def observation_scope(
    stage: ObservationStage,
    *,
    agent: str | None = None,
    iteration: int | None = None,
) -> Iterator[ObservationContext | None]:
    """현재 taskId 를 유지하며 단계·Agent·반복 정보를 좁힌다."""

    current = _CURRENT.get()
    if current is None:
        yield None
        return

    scoped = replace(
        current,
        stage=stage,
        agent=agent if agent is not None else current.agent,
        iteration=iteration if iteration is not None else current.iteration,
    )
    token = _CURRENT.set(scoped)
    try:
        yield scoped
    finally:
        _CURRENT.reset(token)


def emit_observation(
    event_type: ObservationEventType,
    *,
    payload: dict[str, Any] | None = None,
    stage: ObservationStage | None = None,
    agent: str | None = None,
    iteration: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    agent_version: str | None = None,
    provider_version: str | None = None,
    duration_ms: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    tool_tokens: int | None = None,
) -> bool:
    """현재 컨텍스트가 있으면 이벤트를 만들어 emit 하고, 없으면 조용히 건너뛴다."""

    current = _CURRENT.get()
    if current is None:
        return False

    event = ObservationEvent(
        task_id=current.task_id,
        stage=stage or current.stage,
        event_type=event_type,
        agent=agent if agent is not None else current.agent,
        iteration=iteration if iteration is not None else current.iteration,
        provider=provider,
        model=model,
        agent_version=agent_version,
        provider_version=provider_version,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        tool_tokens=tool_tokens,
        payload=payload or {},
    )
    return current.observer.emit(event)
