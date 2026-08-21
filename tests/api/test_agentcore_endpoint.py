"""AgentCore Runtime 컨테이너 계약(`/invocations`, `/ping`) 검증.

`/invocations` 가 `POST /v1/timeline` 과 같은 요청을 같은 방식으로 접수하는지,
`/ping` 이 진행 중인 백그라운드 처리 유무에 따라 `Healthy`/`HealthyBusy` 를
구분해 돌려주는지 확인한다.
"""

import pytest
from fastapi.testclient import TestClient

from app.core import inflight
from app.schemas import TaskStatus, TimelineDraft
from app.server import app
from app.services import timeline_runner
from app.services.app_server_client import get_app_server_client
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-agentcore-1"
_WINDOW = {
    "startAt": "2026-06-20T00:00:00+09:00",
    "endAt": "2026-06-21T00:00:00+09:00",
}


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def app_server() -> FakeAppServerClient:
    fake = FakeAppServerClient(
        snapshot=make_snapshot(task_id=_TASK_ID, source_items=default_source_items())
    )
    app.dependency_overrides[get_app_server_client] = lambda: fake
    return fake


@pytest.fixture
def fake_main_agent(monkeypatch):
    draft = TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    async def _run(request, **_kwargs):
        return draft

    monkeypatch.setattr(timeline_runner, "run_main_agent", _run)
    return draft


def test_ping_reports_healthy_when_idle():
    client = TestClient(app)

    response = client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


def test_ping_reports_healthy_busy_while_task_runs():
    # 202 를 돌려준 뒤에도 처리가 남아 있으면 AgentCore 가 컨테이너를 회수하면 안 된다.
    with inflight.track_inflight():
        client = TestClient(app)

        response = client.get("/ping")

    assert response.json() == {"status": "HealthyBusy"}
    # 블록을 벗어나면 다시 유휴다.
    assert inflight.is_busy() is False


def test_track_inflight_decrements_on_error():
    with pytest.raises(RuntimeError):
        with inflight.track_inflight():
            raise RuntimeError("처리 실패")

    assert inflight.inflight_count() == 0


def test_invocations_accepts_timeline_task(app_server, fake_main_agent):
    client = TestClient(app)

    response = client.post(
        "/invocations",
        json={
            "taskId": _TASK_ID,
            "taskToken": "task-token-1",
            "dailyRecordId": 42,
            "window": _WINDOW,
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "taskId": _TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }


def test_invocations_requires_all_fields(app_server):
    client = TestClient(app)

    response = client.post("/invocations", json={"taskId": _TASK_ID})

    assert response.status_code == 422


def test_background_task_marks_runtime_busy(app_server, monkeypatch):
    """백그라운드 처리가 도는 동안 in-flight 로 잡혀야 `/ping` 이 HealthyBusy 가 된다."""

    busy_during_run: list[bool] = []

    async def _run(request, **_kwargs):
        busy_during_run.append(inflight.is_busy())
        return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    monkeypatch.setattr(timeline_runner, "run_main_agent", _run)

    # TestClient 는 응답 반환 전에 BackgroundTasks 를 끝까지 실행한다.
    client = TestClient(app)
    response = client.post(
        "/invocations",
        json={
            "taskId": _TASK_ID,
            "taskToken": "task-token-2",
            "dailyRecordId": 42,
            "window": _WINDOW,
        },
    )

    assert response.status_code == 202
    assert busy_during_run == [True]
    # 처리가 끝나면 유휴로 돌아가 컨테이너가 회수될 수 있어야 한다.
    assert inflight.is_busy() is False
    assert client.get("/ping").json() == {"status": "Healthy"}
