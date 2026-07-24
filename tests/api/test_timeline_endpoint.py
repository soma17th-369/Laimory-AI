"""POST /v1/timeline 엔드포인트 검증.

요청을 202로 접수하고 백그라운드 처리를 시작하는지 확인한다. AI 서버는 작업
상태를 보관하지 않으므로 별도의 GET 조회 API는 제공하지 않는다.
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
from app.services.timeline_repository import (
    NoopTimelineRepository,
    get_timeline_repository,
)
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-endpoint-1"
_WINDOW = {
    "startAt": "2026-06-20T00:00:00+09:00",
    "endAt": "2026-06-21T00:00:00+09:00",
}


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def repo() -> InMemorySourceRepository:
    source_repo = InMemorySourceRepository()
    source_repo.put(
        make_snapshot(task_id=_TASK_ID, source_items=default_source_items())
    )
    app.dependency_overrides[get_source_repository] = lambda: source_repo
    app.dependency_overrides[get_timeline_repository] = NoopTimelineRepository
    return source_repo


@pytest.fixture
def fake_main_agent(monkeypatch):
    draft = TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    async def _run(request):
        return draft

    monkeypatch.setattr(timeline_runner, "run_main_agent", _run)
    monkeypatch.setattr(timeline_runner.settings, "app_server_api_url", None)
    return draft


def test_post_accepts_timeline_task(repo, fake_main_agent):
    client = TestClient(app)

    response = client.post(
        "/v1/timeline",
        json={
            "taskId": _TASK_ID,
            "callbackToken": "callback-token-1",
            "dailyRecordId": 42,
            "window": _WINDOW,
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "taskId": _TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }


def test_post_missing_snapshot_is_still_accepted(fake_main_agent):
    # 백그라운드 실패 여부는 콜백으로 통보되므로 접수 응답은 202다.
    app.dependency_overrides[get_source_repository] = InMemorySourceRepository
    app.dependency_overrides[get_timeline_repository] = NoopTimelineRepository
    client = TestClient(app)

    response = client.post(
        "/v1/timeline",
        json={
            "taskId": "no-such-task",
            "callbackToken": "callback-token-2",
            "dailyRecordId": 42,
            "window": _WINDOW,
        },
    )

    assert response.status_code == 202


def test_get_timeline_task_route_does_not_exist():
    client = TestClient(app)

    response = client.get("/v1/timeline/does-not-exist")

    assert response.status_code == 404


def test_post_requires_all_fields(repo):
    client = TestClient(app)

    response = client.post("/v1/timeline", json={"taskId": _TASK_ID})

    assert response.status_code == 422
