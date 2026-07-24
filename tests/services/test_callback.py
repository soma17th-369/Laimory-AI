"""App Server 콜백 HTTP 계약 검증."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.schemas import TaskStatus, TimelineCallbackPayload
from app.services import callback


def _payload(
    *,
    status: TaskStatus = TaskStatus.SUCCESS,
    error_code: str | None = None,
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
            _payload(
                status=TaskStatus.FAILED,
                error_code="ERROR_1008",
                error="DB 저장 실패",
            ),
        )
    )

    assert sent is True
    assert captured["body"] == {
        "status": "FAILED",
        "errorCode": "ERROR_1008",
        "error": "DB 저장 실패",
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
