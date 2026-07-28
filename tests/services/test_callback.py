"""App Server 콜백 HTTP 계약 검증."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.error_codes import ErrorCode, message_for
from app.schemas import TaskStatus, TimelineCallbackPayload
from app.services import callback


def _payload(
    *,
    status: TaskStatus = TaskStatus.SUCCESS,
    error_code: int | None = None,
    error: str | None = None,
) -> TimelineCallbackPayload:
    return TimelineCallbackPayload(
        status=status,
        error_code=error_code,
        error=error,
    )


def test_callback_posts_required_token_and_status(monkeypatch):
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["token"] = request.headers.get("Callback-Token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        callback.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    sent = asyncio.run(
        callback.send_callback(
            "https://app.example/s/api/v1/",
            "task/한글",
            "callback-secret",
            _payload(),
        )
    )

    assert sent is True
    assert captured["url"] == (
        "https://app.example/s/api/v1/timeline/drafts/"
        "task%2F%ED%95%9C%EA%B8%80/callback"
    )
    assert captured["token"] == "callback-secret"
    assert captured["body"] == {
        "status": "SUCCESS",
        "errorCode": None,
        "error": None,
    }


def test_failed_callback_posts_error_details(monkeypatch):
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        callback.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    sent = asyncio.run(
        callback.send_callback(
            "https://app.example/s/api/v1",
            "task-1",
            "callback-secret",
            TimelineCallbackPayload.failure(ErrorCode.DATABASE_ERROR),
        )
    )

    assert sent is True
    # errorCode 는 정수로 직렬화된다(구 계약의 "ERROR_1008" 문자열이 아니다).
    assert captured["body"] == {
        "status": "FAILED",
        "errorCode": 1302,
        "error": message_for(ErrorCode.DATABASE_ERROR),
    }


def test_callback_http_failure_returns_false(monkeypatch, caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        callback.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    sent = asyncio.run(
        callback.send_callback(
            "https://app.example/s/api/v1",
            "task-1",
            "callback-secret",
            _payload(),
        )
    )

    assert sent is False
    assert "callback-secret" not in caplog.text


def test_callback_http_error_response_returns_false(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        callback.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    sent = asyncio.run(
        callback.send_callback(
            "https://app.example/s/api/v1",
            "task-1",
            "callback-secret",
            _payload(),
        )
    )

    assert sent is False


def test_callback_payload_rejects_processing_status() -> None:
    with pytest.raises(ValidationError):
        _payload(status=TaskStatus.PROCESSING)


def test_success_payload_has_no_error_fields() -> None:
    payload = TimelineCallbackPayload.success()

    assert payload.model_dump(by_alias=True, mode="json") == {
        "status": "SUCCESS",
        "errorCode": None,
        "error": None,
    }


def test_success_callback_rejects_error_fields() -> None:
    """성공인데 오류가 실리면 App Server 가 상태를 잘못 읽는다."""

    with pytest.raises(ValidationError):
        _payload(status=TaskStatus.SUCCESS, error_code=1901, error="왜 여기 있나")


def test_failed_callback_requires_code_and_message() -> None:
    """실패인데 코드나 메시지가 비면 App Server 가 원인을 알 수 없다."""

    with pytest.raises(ValidationError):
        _payload(status=TaskStatus.FAILED)
    with pytest.raises(ValidationError):
        _payload(status=TaskStatus.FAILED, error_code=1901, error="   ")


def test_failure_payload_uses_catalog_message() -> None:
    payload = TimelineCallbackPayload.failure(ErrorCode.PIPELINE_TIMEOUT)

    assert payload.error_code == 1201
    assert payload.error == message_for(ErrorCode.PIPELINE_TIMEOUT)


def test_failed_callback_rejects_unknown_reserved_or_custom_message() -> None:
    with pytest.raises(ValidationError):
        _payload(status=TaskStatus.FAILED, error_code=9999, error="임의 오류")
    with pytest.raises(ValidationError):
        _payload(
            status=TaskStatus.FAILED,
            error_code=int(ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED),
            error=message_for(ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED),
        )
    with pytest.raises(ValidationError):
        _payload(
            status=TaskStatus.FAILED,
            error_code=int(ErrorCode.INTERNAL_ERROR),
            error="원본 예외 메시지",
        )
