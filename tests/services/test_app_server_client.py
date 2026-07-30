"""App Server API 클라이언트 검증.

헤더·URL·재시도·상태코드 해석·토큰 취급이 여기 계약이다. httpx 전송 계층은
`MockTransport` 로 갈아 끼워 실제 네트워크 없이 확인한다.
"""

import httpx
import pytest

from app.core.error_codes import ErrorCode
from app.schemas import TaskStatus, TimelineCallbackPayload
from app.schemas.timeline_result import TimelineResultEvent, TimelineResultRequest
from app.services import app_server_client as module
from app.services.app_server_client import (
    TASK_TOKEN_HEADER,
    AppServerError,
    HttpAppServerClient,
    TaskToken,
)
from app.services.source_contract import SourceBatchError
from tests.fixtures.requests import fixture_raw_id

_BASE = "https://app.example/s/api/v1"
_TASK_ID = "task-1"


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """재시도 대기는 계약이 아니라 운영 파라미터다. 테스트에서는 기다리지 않는다."""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", _instant)


def _client(handler, **overrides) -> HttpAppServerClient:
    """MockTransport 로 응답을 지정한 클라이언트를 만든다."""

    defaults = dict(timeout_sec=1.0, max_attempts=3, backoff_sec=0.0)
    defaults.update(overrides)
    return HttpAppServerClient(
        _BASE,
        transport=httpx.MockTransport(handler),
        **defaults,
    )


def _input_body(**overrides) -> dict:
    body = {
        "taskId": _TASK_ID,
        "recordDate": "2026-07-22",
        "recordTimeZone": "Asia/Seoul",
        "window": {
            "startAt": "2026-07-22T00:00:00+09:00",
            "endAt": "2026-07-23T00:00:00+09:00",
        },
        "sourceItems": [
            {
                "rawId": fixture_raw_id("client-1"),
                "itemType": "STAY",
                "startAt": "2026-07-22T12:00:00+09:00",
                "endAt": None,
                "payload": {"latitude": 37.5, "longitude": 127.0},
            }
        ],
    }
    body.update(overrides)
    return body


def _result_request() -> TimelineResultRequest:
    return TimelineResultRequest(
        events=[
            TimelineResultEvent(
                event_type="MEAL",
                title="점심",
                subtitle=None,
                start_at="2026-07-22T12:00:00+09:00",
                end_at="2026-07-22T13:00:00+09:00",
                source_raw_ids=[fixture_raw_id("client-1")],
            )
        ]
    )


# --- 요청 형태 ---------------------------------------------------------


async def test_fetch_input_calls_contract_path_with_token_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_input_body())

    snapshot = await _client(handler).fetch_input(_TASK_ID, TaskToken("tok-1"))

    assert seen[0].method == "GET"
    assert str(seen[0].url) == f"{_BASE}/timeline/drafts/{_TASK_ID}/input"
    assert seen[0].headers[TASK_TOKEN_HEADER] == "tok-1"
    assert snapshot.task_id == _TASK_ID
    assert len(snapshot.source_items) == 1


async def test_task_id_is_url_encoded():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_input_body(taskId="a/b?c"))

    await _client(handler).fetch_input("a/b?c", TaskToken("tok-1"))

    assert str(seen[0].url) == f"{_BASE}/timeline/drafts/a%2Fb%3Fc/input"


async def test_submit_result_posts_events_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _client(handler).submit_result(
        _TASK_ID, TaskToken("tok-1"), _result_request()
    )

    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{_BASE}/timeline/drafts/{_TASK_ID}/result"
    assert seen[0].headers[TASK_TOKEN_HEADER] == "tok-1"
    assert b'"eventType":"MEAL"' in seen[0].content.replace(b" ", b"")


async def test_callback_posts_status_body_and_returns_true():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    sent = await _client(handler).send_callback(
        _TASK_ID, TaskToken("tok-1"), TimelineCallbackPayload.success()
    )

    assert sent is True
    assert str(seen[0].url) == f"{_BASE}/timeline/drafts/{_TASK_ID}/callback"
    assert b'"status":"SUCCESS"' in seen[0].content.replace(b" ", b"")


async def test_result_200_without_body_is_success():
    """계약상 결과 저장 성공 응답에는 body 가 없다."""

    await _client(lambda request: httpx.Response(200)).submit_result(
        _TASK_ID, TaskToken("tok-1"), _result_request()
    )


# --- 토큰 -------------------------------------------------------------


async def test_success_response_body_refreshes_token():
    token = TaskToken("tok-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_input_body(taskToken="tok-2"))

    await _client(handler).fetch_input(_TASK_ID, token)

    assert token.value == "tok-2"
    assert token.refresh_count == 1


async def test_error_response_body_does_not_refresh_token():
    """거절된 흐름이 준 토큰을 물면 다음 요청까지 오염된다."""

    token = TaskToken("tok-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"taskToken": "tok-evil"})

    with pytest.raises(AppServerError):
        await _client(handler).fetch_input(_TASK_ID, token)

    assert token.value == "tok-1"


async def test_retry_sends_the_refreshed_token_and_same_body():
    seen: list[httpx.Request] = []
    token = TaskToken("tok-1")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200)

    await _client(handler).submit_result(_TASK_ID, token, _result_request())

    assert len(seen) == 2
    assert seen[0].content == seen[1].content
    assert seen[1].headers[TASK_TOKEN_HEADER] == "tok-1"


def test_task_token_repr_masks_the_value():
    token = TaskToken("tok-secret")

    assert "tok-secret" not in repr(token)
    assert "tok-secret" not in str(token)
    assert "tok-secret" not in f"{token}"


def test_task_token_rejects_empty_value():
    with pytest.raises(ValueError):
        TaskToken("")


# --- 실패 분류 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, ErrorCode.APP_SERVER_UNAUTHORIZED),
        (404, ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND),
        (409, ErrorCode.APP_SERVER_CONFLICT),
    ],
)
async def test_input_abort_statuses_are_not_retried(status_code, expected):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code)

    with pytest.raises(AppServerError) as caught:
        await _client(handler).fetch_input(_TASK_ID, TaskToken("tok-1"))

    assert caught.value.code is expected
    assert caught.value.abort is True
    assert len(seen) == 1


async def test_result_404_uses_task_not_found_code():
    """입력 조회 404(1101)와 그 밖의 404(1405)를 구분한다."""

    with pytest.raises(AppServerError) as caught:
        await _client(lambda request: httpx.Response(404)).submit_result(
            _TASK_ID, TaskToken("tok-1"), _result_request()
        )

    assert caught.value.code is ErrorCode.APP_SERVER_TASK_NOT_FOUND


async def test_server_error_is_retried_up_to_max_attempts():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    with pytest.raises(AppServerError) as caught:
        await _client(handler).fetch_input(_TASK_ID, TaskToken("tok-1"))

    assert len(seen) == 3
    assert caught.value.code is ErrorCode.SOURCE_FETCH_FAILED
    assert caught.value.abort is False


async def test_timeout_is_retried_then_reported_as_failure():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(AppServerError) as caught:
        await _client(handler).submit_result(
            _TASK_ID, TaskToken("tok-1"), _result_request()
        )

    assert attempts["count"] == 3
    assert caught.value.code is ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED


async def test_client_error_is_not_retried():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400)

    with pytest.raises(AppServerError) as caught:
        await _client(handler).submit_result(
            _TASK_ID, TaskToken("tok-1"), _result_request()
        )

    assert len(seen) == 1
    # 400 은 우리가 보낸 것이 잘못된 경우다. 중단이 아니라 실패라서 콜백은 나간다.
    assert caught.value.abort is False


async def test_callback_failure_returns_false_instead_of_raising():
    sent = await _client(lambda request: httpx.Response(500)).send_callback(
        _TASK_ID, TaskToken("tok-1"), TimelineCallbackPayload.success()
    )

    assert sent is False


async def test_callback_does_not_log_the_token(caplog):
    with caplog.at_level("DEBUG"):
        await _client(lambda request: httpx.Response(500)).send_callback(
            _TASK_ID, TaskToken("tok-secret"), TimelineCallbackPayload.success()
        )

    assert "tok-secret" not in caplog.text


# --- 응답 계약 ---------------------------------------------------------


async def test_malformed_input_response_is_a_contract_violation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"taskId": _TASK_ID, "sourceItems": "nope"})

    with pytest.raises(SourceBatchError) as caught:
        await _client(handler).fetch_input(_TASK_ID, TaskToken("tok-1"))

    assert caught.value.code is ErrorCode.SOURCE_CONTRACT_VIOLATION


async def test_empty_source_items_is_a_contract_violation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_input_body(sourceItems=[]))

    with pytest.raises(SourceBatchError):
        await _client(handler).fetch_input(_TASK_ID, TaskToken("tok-1"))


async def test_callback_payload_never_carries_the_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _client(handler).send_callback(
        _TASK_ID,
        TaskToken("tok-secret"),
        TimelineCallbackPayload.failure(ErrorCode.PIPELINE_TIMEOUT),
    )

    body = seen[0].content.decode()
    assert "tok-secret" not in body
    assert "tok-secret" not in str(seen[0].url)
    assert TaskStatus.FAILED.value in body
