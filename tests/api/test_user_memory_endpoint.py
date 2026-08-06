"""POST /v1/user-memory 엔드포인트 검증 (#64).

접수는 **스키마만 맞으면 항상 202** 다. 크기로 거절하지 않는다 — App Server 는 4xx 를
"미접수 확정" 으로 읽고 앱에 502 를 주므로, 이벤트가 많은 정상적인 하루가 사용자에게
저장 실패로 보이게 된다.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.error_codes import ErrorCode
from app.schemas import TaskStatus
from app.schemas.user_memory import UserMemory
from app.server import app
from app.services import user_memory_runner
from app.services.app_server_client import get_app_server_client
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.user_memory import (
    TASK_ID,
    diary,
    diary_event,
    memory_body,
    update_body,
)


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> FakeAppServerClient:
    fake = FakeAppServerClient()
    app.dependency_overrides[get_app_server_client] = lambda: fake
    return fake


@pytest.fixture(autouse=True)
def stub_agent(monkeypatch):
    """실제 LLM 을 부르지 않고 고정 갱신본을 돌려준다."""

    class _Agent:
        def generate(self, existing, digest, *, violations=()):
            return UserMemory(basic_profile="30대 개발자입니다.")

    monkeypatch.setattr(user_memory_runner, "UserMemoryAgent", lambda: _Agent())


def test_post_accepts_the_update_task(client):
    http = TestClient(app)

    response = http.post("/v1/user-memory", json=update_body())

    assert response.status_code == 202
    assert response.json() == {
        "taskId": TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }


def test_accepted_request_sends_exactly_one_result_call(client):
    http = TestClient(app)

    http.post("/v1/user-memory", json=update_body())

    assert client.order == ["user-memory-result"]
    assert client.last_user_memory.status is TaskStatus.SUCCESS


def test_existing_memory_is_optional(client):
    http = TestClient(app)

    for body in (update_body(), update_body(userMemory=memory_body())):
        assert http.post("/v1/user-memory", json=body).status_code == 202


@pytest.mark.parametrize(
    "events",
    [
        pytest.param([diary_event(end_at=None)], id="null-endAt"),
        pytest.param([diary_event(event_type="새로운타입")], id="unknown-eventType"),
        pytest.param(
            [diary_event(subtitle=None, question=None, memo=None)],
            id="all-optionals-null",
        ),
    ],
)
def test_loose_fields_are_accepted(client, events):
    """느슨하게 받는 것이 계약이다. 여기서 422 를 내면 하루 기록이 저장 실패로 보인다."""

    http = TestClient(app)

    response = http.post("/v1/user-memory", json=update_body(diaries=[diary(events=events)]))

    assert response.status_code == 202


def test_a_big_day_is_accepted_not_rejected(client):
    """이벤트가 많은 하루는 정상이다. 크기는 프롬프트 조립 단계에서 자른다."""

    events = [
        diary_event(start_at=f"2026-08-04T{hour:02d}:00:00+09:00", end_at=None)
        for hour in range(24)
    ] * 5
    http = TestClient(app)

    response = http.post(
        "/v1/user-memory",
        json=update_body(diaries=[diary(events=events)] * 10),
    )

    assert response.status_code == 202
    assert client.last_user_memory.status is TaskStatus.SUCCESS


def test_empty_diaries_are_accepted(client):
    http = TestClient(app)

    assert http.post("/v1/user-memory", json=update_body(diaries=[])).status_code == 202


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(update_body(taskId=""), id="empty-taskId"),
        pytest.param({"taskId": TASK_ID}, id="missing-taskToken"),
        pytest.param(
            update_body(diaries=[diary(events=[diary_event(start_at="어제")])]),
            id="unparsable-startAt",
        ),
    ],
)
def test_broken_contract_returns_the_common_error_shape(client, body):
    """계약 위반은 여전히 422 다. 크기와 달리 이건 고쳐야 나아진다."""

    http = TestClient(app)

    response = http.post("/v1/user-memory", json=body)

    assert response.status_code == 422
    assert response.json()["errorCode"] == int(ErrorCode.REQUEST_VALIDATION_FAILED)
