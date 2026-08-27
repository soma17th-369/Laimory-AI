"""Timeline 처리 한 건(taskId)의 실행 컨텍스트.

상관키는 ``taskId`` 하나다. ``process_timeline_task`` 가 최상위 컨텍스트를 열면
``asyncio.to_thread`` 가 contextvars 를 복사하므로 Event/Timeline/Repair Agent 의
worker thread 와 그 안의 LLM 호출까지 같은 taskId 가 따라간다.

이 모듈은 어떤 관측 제품에도 의존하지 않는다. 여기서 유지하는 값은 두 곳이 쓴다.

- 운영 로그(:mod:`app.core.logging`): 모든 로그 줄에 ``taskId``/``stage``/``agent`` 를
  구조화 필드로 붙여 Elasticsearch 에서 한 task 의 흐름을 따라갈 수 있게 한다.
- Langfuse(:mod:`app.core.langfuse_tracing`): LLM generation 이름과 계층을 현재
  단계로 정한다. 컨텍스트가 없으면 모든 generation 이 ``call-llm`` 으로 퇴화한다.

컨텍스트가 없으면(스크립트·단위 테스트 등) 조회 함수는 ``None`` 을 돌려주고
스코프는 조용히 no-op 이 된다.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Iterator


class ExecutionStage(StrEnum):
    """처리가 진행 중인 단계.

    값 문자열은 Langfuse metadata 의 ``stage`` 와 운영 로그의 ``stage`` 필드로
    그대로 나간다. 기존 대시보드·필터가 깨지므로 값을 바꾸지 않는다.
    """

    REQUEST = "REQUEST"
    MAIN_AGENT = "MAIN_AGENT"
    EVENT_AGENT = "EVENT_AGENT"
    TIMELINE_AGENT = "TIMELINE_AGENT"
    REPAIR_AGENT = "REPAIR_AGENT"
    QUESTION_AGENT = "QUESTION_AGENT"
    #: User Memory 갱신(#64). 타임라인과 별개의 작업이라 같은 단계 목록을 쓰되
    #: 겹치는 값(MAIN_AGENT 등)을 재사용하지 않는다.
    USER_MEMORY_AGENT = "USER_MEMORY_AGENT"
    LLM = "LLM"
    STORAGE = "STORAGE"
    CALLBACK = "CALLBACK"
    FINAL = "FINAL"


@dataclass(frozen=True)
class ExecutionContext:
    """현재 실행 중인 task 와 그 안의 위치."""

    task_id: str
    stage: ExecutionStage = ExecutionStage.REQUEST
    agent: str | None = None
    iteration: int | None = None


_CURRENT: ContextVar[ExecutionContext | None] = ContextVar(
    "timeline_execution_context",
    default=None,
)


#: 이 task 에서 구조화 출력이 실패해 결과를 만들지 못한 Agent 이름들 (#98).
#:
#: warning 문자열을 뒤져 판정하지 않으려고 둔다. `AgentWarning`·`TimelineWarning` 은
#: **LLM 에게 스키마로 실려 나가는** 계약이라, 진단용 필드를 거기 더하면 프롬프트가
#: 커지고 모델이 그 값을 채울 수 있게 된다.
#:
#: 리스트를 담는 이유는 `asyncio.to_thread` 가 contextvars 를 **복사**하기 때문이다.
#: 값을 바꾸면 스레드 안에서만 바뀌지만, 같은 리스트 객체에 append 하면 밖에서도 보인다.
_STRUCTURED_FAILURES: ContextVar[list[str] | None] = ContextVar(
    "timeline_structured_failures",
    default=None,
)


def current_execution_context() -> ExecutionContext | None:
    """현재 실행 컨텍스트를 반환한다(없으면 ``None``)."""

    return _CURRENT.get()


def record_structured_failure(agent: str) -> None:
    """구조화 출력 실패로 결과를 만들지 못한 Agent 를 기록한다 (#98)."""

    failures = _STRUCTURED_FAILURES.get()
    if failures is not None:
        failures.append(agent)


def structured_failures() -> tuple[str, ...]:
    """이 task 에서 구조화 출력이 실패한 Agent 이름들."""

    return tuple(_STRUCTURED_FAILURES.get() or ())


@contextmanager
def execution_context(task_id: str) -> Iterator[ExecutionContext]:
    """Timeline 처리 전체가 공유할 최상위 실행 컨텍스트를 연다."""

    context = ExecutionContext(task_id=task_id)
    token = _CURRENT.set(context)
    failures_token = _STRUCTURED_FAILURES.set([])
    try:
        yield context
    finally:
        _STRUCTURED_FAILURES.reset(failures_token)
        _CURRENT.reset(token)


@contextmanager
def execution_scope(
    stage: ExecutionStage,
    *,
    agent: str | None = None,
    iteration: int | None = None,
) -> Iterator[ExecutionContext | None]:
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


def execution_log_fields() -> dict[str, Any]:
    """현재 컨텍스트를 운영 로그 구조화 필드로 변환한다.

    컨텍스트가 없거나 값이 없는 항목은 넣지 않는다. 로그 줄마다 ``null`` 을 채우면
    Elasticsearch 매핑만 늘고 검색에는 도움이 되지 않는다.
    """

    current = _CURRENT.get()
    if current is None:
        return {}

    fields: dict[str, Any] = {"taskId": current.task_id, "stage": current.stage.value}
    if current.agent is not None:
        fields["agent"] = current.agent
    if current.iteration is not None:
        fields["iteration"] = current.iteration
    return fields
