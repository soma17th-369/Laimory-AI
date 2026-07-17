"""POST/GET /v1/timeline 엔드포인트 검증.

`taskId` 만 받아 202 를 돌려주고, 백그라운드에서 저장소 조회 → 정규화 →
메인 에이전트 실행 후 상태 조회로 결과를 받을 수 있는지 확인한다. 메인
에이전트는 monkeypatch 로 대체하고, 수집 스냅샷은 인메모리 저장소에 시드한다.
"""

import pytest
from fastapi.testclient import TestClient

from app.schemas import TaskStatus, TimelineDraft
from app.server import app
from app.services import timeline_runner
from app.services.source_repository import (
    InMemorySourceRepository,
    get_source_repository,
)
from app.services.task_store import InMemoryTaskStore, get_task_store
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-endpoint-1"


@pytest.fixture
def store() -> InMemoryTaskStore:
    fresh = InMemoryTaskStore()
    app.dependency_overrides[get_task_store] = lambda: fresh
    yield fresh
    app.dependency_overrides.clear()


@pytest.fixture
def repo() -> InMemorySourceRepository:
    fresh = InMemorySourceRepository()
    fresh.put(make_snapshot(task_id=_TASK_ID, source_items=default_source_items()))
    app.dependency_overrides[get_source_repository] = lambda: fresh
    yield fresh


@pytest.fixture
def fake_main_agent(monkeypatch):
    draft = TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    async def _run(request):
        return draft

    monkeypatch.setattr(timeline_runner, "run_main_agent", _run)
    monkeypatch.setattr(timeline_runner.settings, "callback_url", None)
    return draft


def test_post_accepts_and_get_returns_result(store, repo, fake_main_agent):
    client = TestClient(app)

    response = client.post("/v1/timeline", json={"taskId": _TASK_ID})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == TaskStatus.PROCESSING.value
    assert body["taskId"] == _TASK_ID

    # TestClient 는 백그라운드 task 완료까지 기다리므로 조회 시 이미 SUCCESS 다.
    status_res = client.get(f"/v1/timeline/{_TASK_ID}")
    assert status_res.status_code == 200
    status_body = status_res.json()
    assert status_body["status"] == TaskStatus.SUCCESS.value
    assert status_body["result"]["userId"] == "u-1"


def test_post_missing_snapshot_marks_failed(store, fake_main_agent):
    # 저장소를 시드하지 않아(빈 저장소) 스냅샷 조회가 실패한다.
    empty = InMemorySourceRepository()
    app.dependency_overrides[get_source_repository] = lambda: empty
    client = TestClient(app)

    response = client.post("/v1/timeline", json={"taskId": "no-such-task"})
    assert response.status_code == 202

    status_res = client.get("/v1/timeline/no-such-task")
    assert status_res.json()["status"] == TaskStatus.FAILED.value


def test_get_unknown_task_returns_404(store):
    client = TestClient(app)
    res = client.get("/v1/timeline/does-not-exist")
    assert res.status_code == 404
