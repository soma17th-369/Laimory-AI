"""AgentCore Runtime 컨테이너 계약(`/invocations`, `/ping`) 검증.

AgentCore 는 진입점이 `/invocations` 하나뿐이라 요청 종류를 body 의 `requestType` 이
말한다(#89). 여기서 고정하는 것은 네 가지다.

1. envelope 2종(TIMELINE·USER_MEMORY_UPDATE)이 각각 기존 핸들러로 위임된다.
2. envelope 없는 Timeline body 도 계속 받는다. 임시 호환이 아니라 영구 계약이다.
3. 종류는 `requestType` 으로만 갈린다 — payload 모양으로 추측하지 않는다.
4. 접수 엔드포인트가 늘어나면 `requestType` 도 함께 늘어난다(커버리지 가드).

`/ping` 은 진행 중인 백그라운드 처리 유무에 따라 `Healthy`/`HealthyBusy` 를 구분한다.
"""

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.agentcore import InvocationRequestType
from app.core import inflight
from app.core.error_codes import ErrorCode
from app.schemas import TaskStatus, TimelineDraft
from app.schemas.user_memory import UserMemory
from app.server import app
from app.services import timeline_runner, user_memory_runner
from app.services.app_server_client import get_app_server_client
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.requests import default_source_items, make_snapshot
from tests.fixtures.user_memory import TASK_ID as _USER_MEMORY_TASK_ID
from tests.fixtures.user_memory import update_body

_TASK_ID = "task-agentcore-1"
_WINDOW = {
    "startAt": "2026-06-20T00:00:00+09:00",
    "endAt": "2026-06-21T00:00:00+09:00",
}
_TIMELINE_PAYLOAD = {
    "taskId": _TASK_ID,
    "taskToken": "task-token-1",
    "dailyRecordId": 42,
    "window": _WINDOW,
}

#: `/v1` 접수 경로 → 그 경로를 대신하는 `requestType`. 새 접수 엔드포인트를 추가하면
#: 여기와 `InvocationRequestType` 양쪽에 항목이 늘어야 한다.
_ROUTE_TO_REQUEST_TYPE = {
    "/v1/timeline": InvocationRequestType.TIMELINE,
    "/v1/user-memory": InvocationRequestType.USER_MEMORY_UPDATE,
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


@pytest.fixture
def fake_user_memory_agent(monkeypatch):
    """실제 LLM 을 부르지 않고 고정 갱신본을 돌려준다."""

    class _Agent:
        def generate(self, existing, digest, *, violations=()):
            return UserMemory(basic_profile="30대 개발자입니다.")

    monkeypatch.setattr(user_memory_runner, "UserMemoryAgent", lambda: _Agent())


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


def test_invocations_accepts_timeline_envelope(app_server, fake_main_agent):
    client = TestClient(app)

    response = client.post(
        "/invocations",
        json={"requestType": "TIMELINE", "payload": _TIMELINE_PAYLOAD},
    )

    assert response.status_code == 202
    assert response.json() == {
        "taskId": _TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }


def test_invocations_accepts_user_memory_envelope(app_server, fake_user_memory_agent):
    """User Memory 갱신도 같은 진입점으로 접수된다. 결과는 저장 호출 한 번으로 나간다."""

    client = TestClient(app)

    response = client.post(
        "/invocations",
        json={"requestType": "USER_MEMORY_UPDATE", "payload": update_body()},
    )

    assert response.status_code == 202
    assert response.json() == {
        "taskId": _USER_MEMORY_TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }
    # 콜백이 없는 계약이라 결과 저장 호출이 곧 종료 통보다.
    assert app_server.last_user_memory is not None


def test_invocations_accepts_timeline_payload_without_envelope(
    app_server, fake_main_agent
):
    """envelope 없는 Timeline body 는 계속 지원한다.

    전환 기간용 임시 호환이 아니라 `/invocations` 의 두 번째 정식 형태다(#89).
    """

    client = TestClient(app)

    response = client.post("/invocations", json=_TIMELINE_PAYLOAD)

    assert response.status_code == 202
    assert response.json() == {
        "taskId": _TASK_ID,
        "status": TaskStatus.PROCESSING.value,
    }


def test_invocations_requires_all_fields(app_server):
    client = TestClient(app)

    response = client.post("/invocations", json={"taskId": _TASK_ID})

    assert response.status_code == 422


def test_invocations_rejects_unknown_request_type(app_server):
    client = TestClient(app)

    response = client.post(
        "/invocations",
        json={"requestType": "SOMETHING_ELSE", "payload": _TIMELINE_PAYLOAD},
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == ErrorCode.REQUEST_VALIDATION_FAILED


def test_invocations_does_not_guess_type_from_payload_shape(app_server):
    """`requestType` 이 payload 스키마를 결정한다.

    User Memory 모양을 TIMELINE 이라고 말하면 거절한다. payload 안을 뒤져 "이건
    사실 User Memory 인가 보다" 라고 고쳐 읽지 않는다 — 그렇게 하면 선택 필드를
    빠뜨린 요청 하나가 엉뚱한 파이프라인으로 들어간다.
    """

    client = TestClient(app)

    response = client.post(
        "/invocations",
        json={"requestType": "TIMELINE", "payload": update_body()},
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == ErrorCode.REQUEST_VALIDATION_FAILED


#: `requestType` 으로 도달하지 않아도 되는 `/v1` POST 경로와 그 이유.
#:
#: `/v1/timeline/test` 는 **접수 경로가 아니다**(이슈 #102). 202 로 받아 백그라운드로
#: 넘기는 대신 요청 안에서 결과를 돌려주므로 `/invocations` 의 접수 응답 계약
#: (`{taskId, status}`)에 담기지 않고, local/dev 에서만 열리는 경로라 AgentCore 로
#: 운영되는 production 에서는 애초에 닫혀 있다. requestType 을 붙이면 운영 진입점에
#: 테스트 경로가 생긴다.
_NON_INTAKE_V1_ROUTES = {"/v1/timeline/test"}


def test_every_v1_intake_route_is_reachable_by_request_type():
    """`/v1` 접수 경로는 전부 `requestType` 으로 도달할 수 있어야 한다.

    AgentCore 는 `/invocations` 하나만 노출하므로, requestType 없이 추가된 접수
    엔드포인트는 AgentCore 배포에서 그냥 닿지 않는 경로가 된다. 헬스·진단
    (`/ping`·`/health`·`/debug/env`)과 `_NON_INTAKE_V1_ROUTES` 는 대상이 아니다.
    """

    intake_routes = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and "POST" in route.methods
        and route.path.startswith("/v1")
        and route.path not in _NON_INTAKE_V1_ROUTES
    }

    assert intake_routes == set(_ROUTE_TO_REQUEST_TYPE)
    assert set(_ROUTE_TO_REQUEST_TYPE.values()) == set(InvocationRequestType)


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


def test_user_memory_invocation_marks_runtime_busy(app_server, monkeypatch):
    """User Memory 도 `/invocations` 로 들어오므로 같은 idle 계약을 지켜야 한다.

    Timeline 만 in-flight 로 잡히면 User Memory 처리 중에 `/ping` 이 `Healthy` 를
    답하고, AgentCore 가 컨테이너를 회수해 갱신이 통째로 사라진다.
    """

    busy_during_run: list[bool] = []

    class _Agent:
        def generate(self, existing, digest, *, violations=()):
            busy_during_run.append(inflight.is_busy())
            return UserMemory(basic_profile="30대 개발자입니다.")

    monkeypatch.setattr(user_memory_runner, "UserMemoryAgent", lambda: _Agent())

    client = TestClient(app)
    response = client.post(
        "/invocations",
        json={"requestType": "USER_MEMORY_UPDATE", "payload": update_body()},
    )

    assert response.status_code == 202
    assert busy_during_run == [True]
    assert inflight.is_busy() is False
    assert client.get("/ping").json() == {"status": "Healthy"}
