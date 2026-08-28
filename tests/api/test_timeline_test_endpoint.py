"""POST /v1/timeline/test 엔드포인트 검증 (이슈 #102).

이 경로가 지켜야 하는 것은 네 가지다.

1. 요청 안에서 파이프라인을 끝내고 **결과 저장 요청과 같은 구조**로 답한다.
2. App Server 를 한 번도 부르지 않는다(입력 조회·결과 저장·콜백 모두).
3. 제한 시간 계약이 비동기 경로와 같다 — 확정본이 있으면 200, 없으면 1201.
4. 비활성 환경에서는 없는 경로와 같은 404/1003 이다.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.v1.timeline_testing import TIMED_OUT_HEADER, TimelineTestRequest
from app.core.config import settings
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
)
from app.schemas.timeline import TimelineEventDraft
from app.schemas.timeline_input import TimelineInputPayload, TimelineInputResponse
from app.server import app
from app.services import timeline_testing
from app.services.app_server_client import get_app_server_client
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.requests import (
    WINDOW_END,
    WINDOW_START,
    default_source_items,
    fixture_raw_id,
)

_TASK_ID = "task-sync-1"
_PATH = "/v1/timeline/test"
#: `default_source_items()` 의 STAY 항목 rawId. 저장 전 자체검증이 결과의 근거를
#: 입력에서 찾으므로 draft 가 실제 입력 rawId 를 가리켜야 한다.
_STAY_RAW_ID = fixture_raw_id("source-101")


def _body(**overrides) -> dict:
    body = {
        "taskId": _TASK_ID,
        "recordDate": "2026-06-20T22:00:00",
        "recordTimeZone": "Asia/Seoul",
        "window": {"startAt": WINDOW_START, "endAt": WINDOW_END},
        "sourceItems": [
            item.model_dump(by_alias=True, mode="json")
            for item in default_source_items()
        ],
    }
    body.update(overrides)
    return body


def _draft_with_event() -> TimelineDraft:
    return TimelineDraft(
        user_id="u-1",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[
            TimelineEventDraft(
                client_event_id="evt-1",
                event_type=EventType.MEAL,
                title="점심",
                description="근처 식당에서 식사했어요.",
                start_time="2026-06-20T12:00:00+09:00",
                end_time="2026-06-20T13:00:00+09:00",
                confidence=0.8,
                inference_level=InferenceLevel.EVIDENCE_BASED,
                source_refs=[
                    SourceRef(source_type=EventSourceType.STAY, raw_id=_STAY_RAW_ID)
                ],
                question="점심 자리에서 어떤 이야기가 기억에 남았나요?",
            )
        ],
    )


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def app_server() -> FakeAppServerClient:
    """이 경로가 App Server 를 부르지 않는다는 사실을 관찰할 스텁."""

    fake = FakeAppServerClient(snapshot=None)
    app.dependency_overrides[get_app_server_client] = lambda: fake
    return fake


@pytest.fixture
def fake_main_agent(monkeypatch) -> TimelineDraft:
    draft = _draft_with_event()

    async def _run(request, **_kwargs):
        return draft

    monkeypatch.setattr(timeline_testing, "run_main_agent", _run)
    return draft


def test_returns_the_result_save_request_shape(app_server, fake_main_agent):
    """응답 body 는 App Server 결과 저장 요청과 같은 구조다."""

    http = TestClient(app)

    response = http.post(_PATH, json=_body())

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"events"}
    [event] = payload["events"]
    assert event["eventType"] == EventType.MEAL.value
    assert event["title"] == "점심"
    assert event["subtitle"] == "근처 식당에서 식사했어요."
    assert event["sourceRawIds"] == [_STAY_RAW_ID]
    assert event["question"] == "점심 자리에서 어떤 이야기가 기억에 남았나요?"
    assert TIMED_OUT_HEADER not in response.headers


def test_does_not_touch_the_app_server(app_server, fake_main_agent):
    """입력 조회·결과 저장·완료 콜백 중 어느 것도 일어나지 않는다."""

    http = TestClient(app)

    http.post(_PATH, json=_body())

    assert app_server.order == []
    assert app_server.fetch_calls == []
    assert app_server.submit_calls == []
    assert app_server.callback_calls == []


def test_timeout_with_a_confirmed_draft_returns_that_draft(monkeypatch, app_server):
    """제한 시간이 끝나도 확정본이 있으면 그것을 200 으로 돌려준다(#76)."""

    draft = _draft_with_event()

    async def _run(request, *, on_confirm=None, **_kwargs):
        if on_confirm is not None:
            on_confirm(draft)
        await asyncio.sleep(5)
        raise AssertionError("취소되지 않았습니다")

    monkeypatch.setattr(timeline_testing, "run_main_agent", _run)
    monkeypatch.setattr(settings, "pipeline_timeout_sec", 0.05)
    http = TestClient(app)

    response = http.post(_PATH, json=_body())

    assert response.status_code == 200
    assert response.headers[TIMED_OUT_HEADER] == "true"
    assert len(response.json()["events"]) == 1


def test_timeout_without_a_confirmed_draft_reports_1201(monkeypatch, app_server):
    """확정본이 하나도 없으면 파이프라인 timeout 실패다."""

    async def _run(request, **_kwargs):
        await asyncio.sleep(5)
        raise AssertionError("취소되지 않았습니다")

    monkeypatch.setattr(timeline_testing, "run_main_agent", _run)
    monkeypatch.setattr(settings, "pipeline_timeout_sec", 0.05)
    http = TestClient(app)

    response = http.post(_PATH, json=_body())

    assert response.status_code == 500
    assert response.json()["errorCode"] == 1201


def test_empty_source_items_is_a_source_contract_violation(app_server, fake_main_agent):
    """묶음 계약 검증도 비동기 경로와 같은 코드를 쓴다."""

    http = TestClient(app)

    response = http.post(_PATH, json=_body(sourceItems=[]))

    assert response.json()["errorCode"] == 1102


def test_request_reuses_the_input_lookup_field_declarations():
    """입력 데이터 필드는 실제 계약(`TimelineInputPayload`)에서 그대로 온다.

    손으로 다시 적으면 한쪽만 고쳐져 두 입구가 갈린다. 상속 관계로 그걸 막는다.
    """

    assert issubclass(TimelineTestRequest, TimelineInputPayload)
    assert issubclass(TimelineInputResponse, TimelineInputPayload)
    assert set(TimelineTestRequest.model_fields) == set(
        TimelineInputPayload.model_fields
    )


def test_receives_task_id_but_not_task_token():
    """`taskId` 는 App Server 가 발행해 이 요청이 받는다. `taskToken` 은 쓸 곳이 없다."""

    fields = set(TimelineTestRequest.model_fields)

    assert "task_id" in fields
    assert "task_token" not in fields
    # 토큰은 되부르는 쪽 계약에만 남아 있어야 한다. 쪼갠 목적이 그것이다.
    assert "task_token" in TimelineInputResponse.model_fields


def test_blank_task_id_is_rejected(app_server, fake_main_agent):
    """빈 taskId 는 상관키가 되지 못한다. 조회 응답 계약과 같은 규칙이다."""

    http = TestClient(app)

    response = http.post(_PATH, json=_body(taskId=""))

    assert response.status_code == 422
    assert response.json()["errorCode"] == 1001


def test_window_is_required_here_but_optional_in_the_lookup_response():
    """이 입구에서만 좁힌 필드다."""

    assert TimelineTestRequest.model_fields["window"].is_required()
    assert not TimelineInputResponse.model_fields["window"].is_required()


def test_task_token_is_ignored_when_sent(app_server, fake_main_agent):
    """입력 조회 응답을 그대로 붙여 넣어도 거절하지 않고 토큰만 무시한다."""

    http = TestClient(app)

    response = http.post(_PATH, json=_body(taskToken="tok-1"))

    assert response.status_code == 200


def test_missing_window_is_a_validation_error(app_server, fake_main_agent):
    """`window` 는 이 경로에서만 필수로 좁힌 필드다."""

    body = _body()
    del body["window"]
    http = TestClient(app)

    response = http.post(_PATH, json=body)

    assert response.status_code == 422
    assert response.json()["errorCode"] == 1001


def test_disabled_environment_hides_the_route(monkeypatch, app_server, fake_main_agent):
    """비활성 환경에서는 없는 경로와 같은 404/1003 이다."""

    monkeypatch.setattr(settings, "timeline_test_enabled", False)
    http = TestClient(app)

    response = http.post(_PATH, json=_body())

    assert response.status_code == 404
    assert response.json()["errorCode"] == 1003


def test_openapi_exposure_follows_the_environment_setting():
    """OpenAPI 노출도 같은 설정을 따른다(import 시점 값)."""

    [route] = [
        route
        for route in app.routes
        if getattr(route, "path", None) == _PATH
    ]

    assert route.include_in_schema is settings.timeline_test_endpoint_enabled


def test_async_timeline_route_is_untouched():
    """접수 경로가 이 변경으로 사라지거나 겹치지 않았다."""

    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/v1/timeline" in paths
    assert "/invocations" in paths
