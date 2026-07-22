"""Elasticsearch 단일 event 인덱스 Bulk exporter 검증."""

import asyncio
import json

import httpx

from app.core.config import settings
from app.core.observability.elasticsearch import export

_EVENT1 = {"@timestamp": "2026-07-23T00:00:01+00:00", "taskId": "t1", "sequence": 1}
_EVENT2 = {"@timestamp": "2026-07-23T00:00:02+00:00", "taskId": "t1", "sequence": 2}


async def _no_sleep(_delay) -> None:
    return None


def _enable_es(monkeypatch, **overrides) -> None:
    monkeypatch.setattr(settings, "es_url", "http://es.test")
    monkeypatch.setattr(settings, "es_api_key", "")
    monkeypatch.setattr(settings, "es_event_index", "ai-timeline-task")
    monkeypatch.setattr(settings, "es_timeout_sec", 1.0)
    monkeypatch.setattr(settings, "es_max_retries", 3)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


async def test_export_sends_valid_ndjson_to_single_index(monkeypatch) -> None:
    _enable_es(monkeypatch)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"errors": False, "items": []})

    await export([_EVENT1], transport=httpx.MockTransport(handler))

    assert captured["content_type"] == "application/x-ndjson"
    assert captured["body"].endswith("\n")
    lines = captured["body"].strip().split("\n")
    assert len(lines) == 2
    action = json.loads(lines[0])["index"]
    assert action == {"_index": "ai-timeline-task-2026.07", "_id": "t1-1"}


async def test_export_noop_without_es_url(monkeypatch) -> None:
    _enable_es(monkeypatch, es_url="")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"errors": False})

    await export([_EVENT1], transport=httpx.MockTransport(handler))
    assert calls["n"] == 0


async def test_export_retries_on_429_then_succeeds(monkeypatch) -> None:
    _enable_es(monkeypatch)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"errors": False, "items": []})

    await export([_EVENT1], transport=httpx.MockTransport(handler))
    assert calls["n"] == 2


async def test_export_does_not_retry_on_permanent_4xx(monkeypatch) -> None:
    _enable_es(monkeypatch)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    await export([_EVENT1], transport=httpx.MockTransport(handler))
    assert calls["n"] == 1


async def test_export_retries_only_retryable_items(monkeypatch) -> None:
    _enable_es(monkeypatch, es_max_retries=2)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode("utf-8"))
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "errors": True,
                    "items": [
                        {"index": {"status": 429}},
                        {"index": {"status": 400, "error": {"type": "mapping"}}},
                    ],
                },
            )
        return httpx.Response(200, json={"errors": False, "items": []})

    await export([_EVENT1, _EVENT2], transport=httpx.MockTransport(handler))

    assert len(bodies) == 2
    assert '"_id": "t1-1"' in bodies[1]
    assert '"_id": "t1-2"' not in bodies[1]


async def test_export_isolates_connection_error(monkeypatch) -> None:
    _enable_es(monkeypatch, es_max_retries=1)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("es down")

    await export([_EVENT1], transport=httpx.MockTransport(handler))
