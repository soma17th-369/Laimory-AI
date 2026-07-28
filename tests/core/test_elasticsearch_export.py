"""Elasticsearch 단일 event 인덱스 Bulk exporter 검증."""

import asyncio
import json
import logging

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


async def test_export_sends_valid_ndjson_to_single_index(monkeypatch, caplog) -> None:
    _enable_es(monkeypatch)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"errors": False, "items": []})

    with caplog.at_level(
        logging.INFO,
        logger="app.core.observability.elasticsearch",
    ):
        result = await export([_EVENT1], transport=httpx.MockTransport(handler))

    assert captured["content_type"] == "application/x-ndjson"
    assert captured["body"].endswith("\n")
    lines = captured["body"].strip().split("\n")
    assert len(lines) == 2
    action = json.loads(lines[0])["index"]
    assert action == {"_index": "ai-timeline-task-2026.07", "_id": "t1-1"}
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert "관측 ES 전송 시작: taskId=t1" in caplog.text
    assert "attempted=1, succeeded=1, failed=0" in caplog.text


async def test_export_noop_without_es_url(monkeypatch, caplog) -> None:
    _enable_es(monkeypatch, es_url="")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"errors": False})

    result = await export(
        [_EVENT1],
        task_id="t1",
        transport=httpx.MockTransport(handler),
    )

    assert calls["n"] == 0
    assert result.attempted == 0
    assert result.succeeded == 0
    assert result.failed == 0
    assert "reason=es_url_not_configured" in caplog.text


async def test_export_retries_on_429_then_succeeds(monkeypatch) -> None:
    _enable_es(monkeypatch)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"errors": False, "items": []})

    result = await export([_EVENT1], transport=httpx.MockTransport(handler))

    assert calls["n"] == 2
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0


async def test_export_does_not_retry_on_permanent_4xx(monkeypatch) -> None:
    _enable_es(monkeypatch)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    result = await export([_EVENT1], transport=httpx.MockTransport(handler))

    assert calls["n"] == 1
    assert result.attempted == 1
    assert result.succeeded == 0
    assert result.failed == 1


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

    result = await export(
        [_EVENT1, _EVENT2],
        transport=httpx.MockTransport(handler),
    )

    assert len(bodies) == 2
    assert '"_id": "t1-1"' in bodies[1]
    assert '"_id": "t1-2"' not in bodies[1]
    assert result.attempted == 2
    assert result.succeeded == 1
    assert result.failed == 1


async def test_export_isolates_connection_error(monkeypatch, caplog) -> None:
    _enable_es(monkeypatch, es_max_retries=1)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("es down")

    result = await export([_EVENT1], transport=httpx.MockTransport(handler))

    assert result.attempted == 1
    assert result.succeeded == 0
    assert result.failed == 1
    assert "관측 ES 재시도 소진: errorCode=1402, documents=1" in caplog.text
    assert "attempted=1, succeeded=0, failed=1" in caplog.text
