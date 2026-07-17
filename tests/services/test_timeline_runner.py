"""백그라운드 처리부(process_timeline_task) 검증.

수집 스냅샷 조회 → 정규화 → 메인 에이전트 실행의 성공/실패/timeout 상태 전이와,
설정된 콜백 URL 로의 전송 여부를 확인한다. 메인 에이전트와 콜백 전송은
monkeypatch 로 대체하고, 스냅샷은 인메모리 저장소에 시드한다.
"""

import asyncio

from app.core.observability import (
    ContentCapture,
    InMemoryObservationSink,
    Observer,
)
from app.schemas import TaskStatus, TimelineDraft
from app.services import timeline_runner
from app.services.source_repository import InMemorySourceRepository
from app.services.task_store import InMemoryTaskStore
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-1"


def _draft() -> TimelineDraft:
    return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")


def _seeded_repo(task_id=_TASK_ID) -> InMemorySourceRepository:
    repo = InMemorySourceRepository()
    repo.put(make_snapshot(task_id=task_id, source_items=default_source_items()))
    return repo


def _run(store: InMemoryTaskStore, repo: InMemorySourceRepository, task_id=_TASK_ID):
    store.create(task_id, task_id)
    asyncio.run(timeline_runner.process_timeline_task(task_id, store, repo))
    return store.get(task_id)


def _patch_callback(monkeypatch) -> list:
    sent = []

    async def fake_send(url, payload):
        sent.append((url, payload))
        return True

    monkeypatch.setattr(timeline_runner, "send_callback", fake_send)
    return sent


def test_success_marks_success_and_sends_callback(monkeypatch):
    draft = _draft()

    async def fake_main_agent(request):
        return draft

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(timeline_runner.settings, "callback_url", "https://app.example/cb")
    sent = _patch_callback(monkeypatch)

    record = _run(InMemoryTaskStore(), _seeded_repo())

    assert record.status is TaskStatus.SUCCESS
    assert record.result is draft
    assert len(sent) == 1
    assert sent[0][0] == "https://app.example/cb"
    assert sent[0][1].status is TaskStatus.SUCCESS


def test_transaction_id_reaches_request_and_all_runner_observations(monkeypatch):
    transaction_id = "tx-runner-1"
    captured_requests = []
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)
    store = InMemoryTaskStore()
    store.create(_TASK_ID, transaction_id)

    async def fake_main_agent(request):
        captured_requests.append(request)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(timeline_runner.settings, "callback_url", None)

    asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID,
            store,
            _seeded_repo(),
            observer=observer,
        )
    )

    assert captured_requests[0].transaction_id == transaction_id
    assert sink.events
    assert {event.transaction_id for event in sink.events} == {transaction_id}


def test_no_callback_url_skips_callback(monkeypatch):
    async def fake_main_agent(request):
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(timeline_runner.settings, "callback_url", None)
    sent = _patch_callback(monkeypatch)

    record = _run(InMemoryTaskStore(), _seeded_repo())

    assert record.status is TaskStatus.SUCCESS
    assert sent == []


def test_missing_snapshot_marks_failed(monkeypatch):
    monkeypatch.setattr(timeline_runner.settings, "callback_url", None)
    _patch_callback(monkeypatch)

    # 저장소를 비워 두면 스냅샷 조회가 실패한다.
    record = _run(InMemoryTaskStore(), InMemorySourceRepository())

    assert record.status is TaskStatus.FAILED
    assert record.result is None
    assert "찾지 못" in record.error


def test_exception_marks_failed_without_result(monkeypatch):
    async def boom(request):
        raise RuntimeError("메인 에이전트 폭발")

    monkeypatch.setattr(timeline_runner, "run_main_agent", boom)
    monkeypatch.setattr(timeline_runner.settings, "callback_url", "https://app.example/cb")
    sent = _patch_callback(monkeypatch)

    record = _run(InMemoryTaskStore(), _seeded_repo())

    assert record.status is TaskStatus.FAILED
    assert record.result is None
    assert "메인 에이전트 폭발" in record.error
    # 실패도 콜백으로 알린다.
    assert sent and sent[0][1].status is TaskStatus.FAILED


def test_timeout_marks_failed(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", slow)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)
    monkeypatch.setattr(timeline_runner.settings, "callback_url", None)
    _patch_callback(monkeypatch)

    record = _run(InMemoryTaskStore(), _seeded_repo())

    assert record.status is TaskStatus.FAILED
    assert record.result is None
    assert "timeout" in record.error.lower()
