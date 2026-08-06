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
from app.schemas.user_memory_update import DailyTimeline, UserMemoryUpdateRequest
from app.server import app
from app.services import user_memory_runner
from app.services.app_server_client import get_app_server_client
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.user_memory import (
    TASK_ID,
    daily_timeline,
    daily_timeline_event,
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
        pytest.param([daily_timeline_event(end_at=None)], id="null-endAt"),
        pytest.param([daily_timeline_event(event_type="새로운타입")], id="unknown-eventType"),
        pytest.param(
            [daily_timeline_event(subtitle=None, question=None, memo=None)],
            id="all-optionals-null",
        ),
    ],
)
def test_loose_fields_are_accepted(client, events):
    """느슨하게 받는 것이 계약이다. 여기서 422 를 내면 하루 기록이 저장 실패로 보인다."""

    http = TestClient(app)

    response = http.post("/v1/user-memory", json=update_body(dailyTimelines=[daily_timeline(events=events)]))

    assert response.status_code == 202


def test_a_big_day_is_accepted_not_rejected(client):
    """이벤트가 많은 하루는 정상이다. 크기는 프롬프트 조립 단계에서 자른다."""

    events = [
        daily_timeline_event(start_at=f"2026-08-04T{hour:02d}:00:00+09:00", end_at=None)
        for hour in range(24)
    ] * 5
    http = TestClient(app)

    response = http.post(
        "/v1/user-memory",
        json=update_body(dailyTimelines=[daily_timeline(events=events)] * 10),
    )

    assert response.status_code == 202
    assert client.last_user_memory.status is TaskStatus.SUCCESS


def test_empty_daily_timelines_are_accepted(client):
    http = TestClient(app)

    assert http.post("/v1/user-memory", json=update_body(dailyTimelines=[])).status_code == 202


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(update_body(taskId=""), id="empty-taskId"),
        pytest.param({"taskId": TASK_ID}, id="missing-taskToken"),
        pytest.param(
            update_body(dailyTimelines=[daily_timeline(events=[daily_timeline_event(start_at="어제")])]),
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


# --- 계약 키 고정 -------------------------------------------------------


def test_wire_key_is_daily_timelines():
    """`populate_by_name` 때문에 snake_case 도 파싱된다. 계약 키는 하나다.

    이 단언이 없으면 필드 이름을 바꿔도 모든 테스트가 그대로 통과하고, 실제 App
    Server 요청만 조용히 빈 목록으로 들어온다.
    """

    field = UserMemoryUpdateRequest.model_fields["daily_timelines"]

    assert field.alias == "dailyTimelines"


def test_daily_timeline_wire_key_is_record_date():
    """App Server의 `recordDate`를 공개 계약의 유일한 날짜 키로 고정한다."""

    field = DailyTimeline.model_fields["record_date"]
    properties = DailyTimeline.model_json_schema(by_alias=True)["properties"]

    assert field.alias == "recordDate"
    assert "recordDate" in properties
    assert "date" not in properties


def test_record_date_is_accepted(client):
    http = TestClient(app)

    response = http.post(
        "/v1/user-memory",
        json=update_body(
            dailyTimelines=[daily_timeline(record_date="2026-07-08")]
        ),
    )

    assert response.status_code == 202
    assert client.last_user_memory.status is TaskStatus.SUCCESS


def test_old_date_key_is_rejected(client):
    """날짜 키를 둘 다 받으면 생산자마다 계약이 다시 갈리므로 구 키는 거절한다."""

    http = TestClient(app)
    timeline = daily_timeline()
    timeline["date"] = timeline.pop("recordDate")

    response = http.post(
        "/v1/user-memory",
        json=update_body(dailyTimelines=[timeline]),
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == int(
        ErrorCode.REQUEST_VALIDATION_FAILED
    )


def test_old_diaries_key_no_longer_populates(client):
    """예전 이름으로 오면 빈 목록이다. 조용히 둘 다 받아 주지 않는다."""

    http = TestClient(app)
    body = update_body()
    body["diaries"] = body.pop("dailyTimelines")

    response = http.post("/v1/user-memory", json=body)

    assert response.status_code == 202
    assert client.last_user_memory.status is TaskStatus.SUCCESS
