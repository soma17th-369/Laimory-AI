"""taskId 관측 컨텍스트의 to_thread 전파와 요청 간 격리 검증."""

import asyncio

from app.core.observability import (
    InMemoryObservationSink,
    ObservationEventType,
    ObservationStage,
    Observer,
    current_observation_context,
    emit_observation,
    observation_context,
    observation_scope,
)


def test_emit_without_context_is_noop() -> None:
    # 컨텍스트가 열려 있지 않으면(스크립트·단위 테스트) 조용히 건너뛴다.
    assert emit_observation(ObservationEventType.STARTED) is False


def test_scope_is_available_without_elasticsearch_observer() -> None:
    """Langfuse용 stage·agent 문맥은 ES observer가 없어도 유지된다."""

    with observation_context("task-langfuse-only", None):
        with observation_scope(
            ObservationStage.EVENT_AGENT,
            agent="sleep_activity",
        ):
            current = current_observation_context()
            assert current is not None
            assert current.task_id == "task-langfuse-only"
            assert current.stage is ObservationStage.EVENT_AGENT
            assert current.agent == "sleep_activity"
            assert emit_observation(ObservationEventType.STARTED) is False


def test_context_propagates_into_to_thread() -> None:
    """asyncio.to_thread 로 넘어간 worker thread 에서도 같은 taskId 로 emit 된다."""

    sink = InMemoryObservationSink()
    observer = Observer(sink)

    def worker() -> None:
        # 다른 스레드 — contextvars 복사로 taskId/stage/agent 가 이어져야 한다.
        with observation_scope(ObservationStage.EVENT_AGENT, agent="location"):
            emit_observation(ObservationEventType.STARTED)

    async def run() -> None:
        with observation_context("task-xyz", observer):
            await asyncio.gather(
                asyncio.to_thread(worker),
                asyncio.to_thread(worker),
                asyncio.to_thread(worker),
            )

    asyncio.run(run())

    assert len(sink.events) == 3
    assert {e.task_id for e in sink.events} == {"task-xyz"}
    assert all(
        e.stage is ObservationStage.EVENT_AGENT and e.agent == "location"
        for e in sink.events
    )
    # 병렬 emit 이라도 sequence 는 겹치지 않는다.
    assert sorted(e.sequence for e in sink.events) == [0, 1, 2]


def test_concurrent_tasks_have_isolated_context() -> None:
    """동시에 도는 두 task 의 Observer/버퍼가 섞이지 않는다(요청별 인스턴스)."""

    sink_a = InMemoryObservationSink()
    sink_b = InMemoryObservationSink()
    observer_a = Observer(sink_a)
    observer_b = Observer(sink_b)

    async def one(task_id: str, observer: Observer) -> None:
        with observation_context(task_id, observer):
            # to_thread 안에서 emit 해도 자기 task 의 observer 로만 간다.
            await asyncio.to_thread(
                lambda: emit_observation(
                    ObservationEventType.STARTED, stage=ObservationStage.MAIN_AGENT
                )
            )

    async def run() -> None:
        await asyncio.gather(
            one("task-a", observer_a),
            one("task-b", observer_b),
        )

    asyncio.run(run())

    assert [e.task_id for e in sink_a.events] == ["task-a"]
    assert [e.task_id for e in sink_b.events] == ["task-b"]
