"""App Server 콜백 HTTP 계약 검증."""

import asyncio
import json

import httpx

from app.schemas import TaskStatus, TimelineCallbackPayload
from app.services import callback


def _payload() -> TimelineCallbackPayload:
    return TimelineCallbackPayload(
        task_id="task-1",
        callback_token="callback-secret",
        status=TaskStatus.SUCCESS,
    )


def test_callback_posts_required_token_and_status(monkeypatch):
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        callback.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    sent = asyncio.run(callback.send_callback("https://app.example/callback", _payload()))

    assert sent is True
    assert captured["url"] == "https://app.example/callback"
    assert captured["body"] == {
        "taskId": "task-1",
        "callbackToken": "callback-secret",
        "status": "SUCCESS",
    }


def test_callback_http_failure_returns_false(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        callback.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    sent = asyncio.run(callback.send_callback("https://app.example/callback", _payload()))

    assert sent is False
