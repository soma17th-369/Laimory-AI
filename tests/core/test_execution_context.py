"""taskId 실행 컨텍스트의 to_thread 전파와 요청 간 격리 검증."""

import asyncio

from app.core.execution_context import (
    ExecutionStage,
    current_execution_context,
    execution_context,
    execution_log_fields,
    execution_scope,
)


def test_no_context_yields_no_fields() -> None:
    # 컨텍스트가 열려 있지 않으면(스크립트·단위 테스트) 조용히 비어 있다.
    assert current_execution_context() is None
    assert execution_log_fields() == {}


def test_scope_narrows_stage_and_agent() -> None:
    """Langfuse generation 이름과 로그 stage 가 같은 컨텍스트에서 나온다."""

    with execution_context("task-scope"):
        with execution_scope(ExecutionStage.EVENT_AGENT, agent="sleep_activity"):
            current = current_execution_context()
            assert current is not None
            assert current.task_id == "task-scope"
            assert current.stage is ExecutionStage.EVENT_AGENT
            assert current.agent == "sleep_activity"
            assert execution_log_fields() == {
                "taskId": "task-scope",
                "stage": "EVENT_AGENT",
                "agent": "sleep_activity",
            }

        # 스코프를 벗어나면 상위 컨텍스트로 돌아온다.
        restored = current_execution_context()
        assert restored is not None
        assert restored.stage is ExecutionStage.REQUEST
        assert restored.agent is None


def test_scope_without_context_is_noop() -> None:
    """컨텍스트 밖에서 스코프를 열어도 터지지 않는다(스크립트 실행 경로)."""

    with execution_scope(ExecutionStage.LLM) as scoped:
        assert scoped is None


def test_context_propagates_into_to_thread() -> None:
    """asyncio.to_thread 로 넘어간 worker thread 도 같은 taskId 를 본다."""

    collected: list[dict[str, object]] = []

    def worker() -> None:
        # 다른 스레드 — contextvars 복사로 taskId/stage/agent 가 이어져야 한다.
        with execution_scope(ExecutionStage.EVENT_AGENT, agent="location"):
            collected.append(execution_log_fields())

    async def run() -> None:
        with execution_context("task-xyz"):
            await asyncio.gather(
                asyncio.to_thread(worker),
                asyncio.to_thread(worker),
                asyncio.to_thread(worker),
            )

    asyncio.run(run())

    assert len(collected) == 3
    assert all(
        fields
        == {"taskId": "task-xyz", "stage": "EVENT_AGENT", "agent": "location"}
        for fields in collected
    )


def test_concurrent_tasks_have_isolated_context() -> None:
    """동시에 도는 두 task 의 컨텍스트가 섞이지 않는다."""

    seen: dict[str, list[object]] = {"task-a": [], "task-b": []}

    async def one(task_id: str) -> None:
        with execution_context(task_id):
            await asyncio.to_thread(
                lambda: seen[task_id].append(execution_log_fields()["taskId"])
            )

    async def run() -> None:
        await asyncio.gather(one("task-a"), one("task-b"))

    asyncio.run(run())

    assert seen == {"task-a": ["task-a"], "task-b": ["task-b"]}
