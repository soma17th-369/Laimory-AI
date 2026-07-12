"""InMemoryTaskStore 상태 전이 검증."""

import pytest

from app.schemas import TaskStatus, TimelineDraft
from app.services.task_store import InMemoryTaskStore


def _draft() -> TimelineDraft:
    return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")


def test_create_starts_in_processing():
    store = InMemoryTaskStore()
    record = store.create("task-1", "tx-1")

    assert record.status is TaskStatus.PROCESSING
    assert record.result is None
    assert store.get("task-1") is record


def test_get_missing_returns_none():
    store = InMemoryTaskStore()
    assert store.get("nope") is None


def test_mark_success_stores_result():
    store = InMemoryTaskStore()
    store.create("task-1", "tx-1")

    draft = _draft()
    record = store.mark_success("task-1", draft)

    assert record.status is TaskStatus.SUCCESS
    assert record.result is draft
    assert record.error is None


def test_mark_failed_clears_partial_result():
    store = InMemoryTaskStore()
    store.create("task-1", "tx-1")
    store.mark_success("task-1", _draft())

    record = store.mark_failed("task-1", "boom")

    assert record.status is TaskStatus.FAILED
    assert record.result is None
    assert record.error == "boom"


def test_mark_missing_task_raises():
    store = InMemoryTaskStore()
    with pytest.raises(KeyError):
        store.mark_success("nope", _draft())
