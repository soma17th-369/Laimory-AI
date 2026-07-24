"""백그라운드 타임라인 처리와 완료 콜백을 검증한다."""

import asyncio

from app.schemas import TaskStatus, TimelineDraft
from app.services import timeline_runner
from app.services.source_repository import InMemorySourceRepository
from app.services.timeline_repository import NoopTimelineRepository
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-1"
_DAILY_RECORD_ID = 42
_WINDOW_START = "2026-06-20T00:00:00+09:00"
_WINDOW_END = "2026-06-21T00:00:00+09:00"


def _draft() -> TimelineDraft:
    return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")


def _seeded_repo(task_id: str = _TASK_ID) -> InMemorySourceRepository:
    repo = InMemorySourceRepository()
    repo.put(make_snapshot(task_id=task_id, source_items=default_source_items()))
    return repo


class _RaisingTimelineRepo:
    """저장 단계에서 항상 예외를 발생시키는 테스트 저장소."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def save(self, task_id, draft, daily_record_id):
        raise self.exc


def _run(
    repo: InMemorySourceRepository,
    task_id: str = _TASK_ID,
    timeline_repo=None,
    callback_token: str = "callback-token",
) -> TaskStatus:
    return asyncio.run(
        timeline_runner.process_timeline_task(
            task_id,
            repo,
            timeline_repo or NoopTimelineRepository(),
            _DAILY_RECORD_ID,
            _WINDOW_START,
            _WINDOW_END,
            callback_token,
        )
    )


def _patch_callback(monkeypatch) -> list:
    sent = []

    async def fake_send(app_server_api_url, task_id, callback_token, payload):
        sent.append((app_server_api_url, task_id, callback_token, payload))
        return True

    monkeypatch.setattr(timeline_runner, "send_callback", fake_send)
    return sent


def test_success_returns_success_and_sends_callback(monkeypatch):
    async def fake_main_agent(request):
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(
        timeline_runner.settings,
        "app_server_api_url",
        "https://app.example/s/api/v1",
    )
    sent = _patch_callback(monkeypatch)

    status = _run(_seeded_repo())

    assert status is TaskStatus.SUCCESS
    assert len(sent) == 1
    assert sent[0][0] == "https://app.example/s/api/v1"
    assert sent[0][1] == _TASK_ID
    assert sent[0][2] == "callback-token"
    assert sent[0][3].status is TaskStatus.SUCCESS
    assert sent[0][3].error_code is None
    assert sent[0][3].error is None


def test_no_app_server_api_url_skips_callback(monkeypatch):
    async def fake_main_agent(request):
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(timeline_runner.settings, "app_server_api_url", None)
    sent = _patch_callback(monkeypatch)

    status = _run(_seeded_repo())

    assert status is TaskStatus.SUCCESS
    assert sent == []


def test_missing_snapshot_returns_failed(monkeypatch):
    monkeypatch.setattr(timeline_runner.settings, "app_server_api_url", None)

    status = _run(InMemorySourceRepository())

    assert status is TaskStatus.FAILED


def test_agent_exception_returns_failed_callback(monkeypatch):
    async def boom(request):
        raise RuntimeError("메인 에이전트 오류")

    monkeypatch.setattr(timeline_runner, "run_main_agent", boom)
    monkeypatch.setattr(
        timeline_runner.settings,
        "app_server_api_url",
        "https://app.example/s/api/v1",
    )
    sent = _patch_callback(monkeypatch)

    status = _run(_seeded_repo())

    assert status is TaskStatus.FAILED
    assert sent[0][3].status is TaskStatus.FAILED
    assert sent[0][3].error_code == "ERROR_1008"
    assert sent[0][3].error == "메인 에이전트 오류"


def test_callback_passes_callback_token_as_transport_argument(monkeypatch):
    async def fake_main_agent(request):
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(
        timeline_runner.settings,
        "app_server_api_url",
        "https://app.example/s/api/v1",
    )
    sent = _patch_callback(monkeypatch)

    status = _run(_seeded_repo(), callback_token="tok-123")

    assert status is TaskStatus.SUCCESS
    assert sent[0][2] == "tok-123"


def test_db_save_failure_returns_failed_callback(monkeypatch):
    async def fake_main_agent(request):
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(
        timeline_runner.settings,
        "app_server_api_url",
        "https://app.example/s/api/v1",
    )
    sent = _patch_callback(monkeypatch)
    failing_repo = _RaisingTimelineRepo(RuntimeError("DB 저장 실패"))

    status = _run(_seeded_repo(), timeline_repo=failing_repo)

    assert status is TaskStatus.FAILED
    assert sent[0][3].status is TaskStatus.FAILED
    assert sent[0][3].error_code == "ERROR_1008"
    assert sent[0][3].error == "DB 저장 실패"


def test_timeout_returns_failed(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", slow)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)
    monkeypatch.setattr(timeline_runner.settings, "app_server_api_url", None)

    status = _run(_seeded_repo())

    assert status is TaskStatus.FAILED
