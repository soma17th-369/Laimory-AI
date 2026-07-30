"""POST /v1/timeline 엔드포인트 검증.

요청을 202로 접수하고 백그라운드 처리를 시작하는지 확인한다. AI 서버는 작업
상태를 보관하지 않으므로 별도의 GET 조회 API는 제공하지 않는다.
"""

import pytest
from fastapi.testclient import TestClient

from app.schemas import TaskStatus, TimelineDraft
from app.server import app
from app.services import timeline_runner
from app.services.app_server_client import get_app_server_client
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-endpoint-1"
_WINDOW = {
    "startAt": "2026-06-20T00:00:00+09:00",
    "endAt": "2026-06-21T00:00:00+09:00",
}


def _payload(**overrides) -> dict:
    body = {
        "taskId": _TASK_ID,
        "taskToken": "task-token-1",
        "dailyRecordId": 42,
        "window": _WINDOW,
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> FakeAppServerClient:
    fake = FakeAppServerClient(
        snapshot=make_snapshot(task_id=_TASK_ID, source_items=default_source_items())
    )
    app.dependency_overrides[get_app_server_client] = lambda: fake
    return fake


@pytest.fixture
def fake_main_agent(monkeypatch):
    draft = TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")

    async def _run(request):
        return draft

    monkeypatch.setattr(timeline_runner, "run_main_agent", _run)
    return draft


def test_post_accepts_timeline_task(client, fake_main_agent):
    http = TestClient(app)

    response = http.post("/v1/timeline", json=_payload())

    assert response.status_code == 202
    assert response.json() == {
        "taskId": _TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }


def test_accepted_request_runs_the_full_app_server_flow(client, fake_main_agent):
    """접수 후 백그라운드가 입력 조회 → 결과 저장 → 콜백을 순서대로 탄다."""

    http = TestClient(app)

    http.post("/v1/timeline", json=_payload(taskToken="tok-flow"))

    assert client.order == ["input", "result", "callback"]
    assert client.callback_calls[0].token == "tok-flow"


def test_post_missing_input_is_still_accepted(fake_main_agent):
    # 백그라운드 실패 여부는 콜백으로 통보되므로 접수 응답은 202다.
    app.dependency_overrides[get_app_server_client] = lambda: FakeAppServerClient(
        snapshot=None
    )
    http = TestClient(app)

    response = http.post("/v1/timeline", json=_payload(taskId="no-such-task"))

    assert response.status_code == 202


def test_get_timeline_task_route_does_not_exist():
    http = TestClient(app)

    response = http.get("/v1/timeline/does-not-exist")

    assert response.status_code == 404


def test_post_requires_all_fields(client):
    http = TestClient(app)

    response = http.post("/v1/timeline", json={"taskId": _TASK_ID})

    assert response.status_code == 422


def test_post_rejects_legacy_callback_token_field(client):
    """구 계약(`callbackToken`)으로는 접수되지 않는다."""

    http = TestClient(app)
    body = _payload()
    body["callbackToken"] = body.pop("taskToken")

    response = http.post("/v1/timeline", json=body)

    assert response.status_code == 422
