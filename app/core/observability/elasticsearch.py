"""Elasticsearch Bulk exporter (httpx, NDJSON).

외부 문서에는 ``taskId`` 하나만 저장한다. Bulk 문서 ``_id``는 taskId와 sequence로
파생해 같은 요청을 재전송할 때 중복 생성을 막는다.
taskId는 App Server가 요청마다 새로 발급하는 일회성 식별자이며 재사용하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Iterator
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_BULK_BYTES = 5 * 1024 * 1024
_Item = tuple[str, str, dict[str, Any]]


def _index_name(base: str, timestamp_iso: str) -> str:
    year_month = timestamp_iso[:7].replace("-", ".")
    return f"{base}-{year_month}"


def _to_items(event_documents: list[dict[str, Any]]) -> list[_Item]:
    items: list[_Item] = []
    for event_document in event_documents:
        task_id = event_document["taskId"]
        sequence = event_document["sequence"]
        items.append(
            (
                _index_name(settings.es_event_index, event_document["@timestamp"]),
                f"{task_id}-{sequence}",
                event_document,
            )
        )
    return items


def _ndjson(items: list[_Item]) -> bytes:
    lines: list[str] = []
    for index, doc_id, source in items:
        lines.append(
            json.dumps(
                {"index": {"_index": index, "_id": doc_id}},
                ensure_ascii=False,
            )
        )
        lines.append(json.dumps(source, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _chunks(items: list[_Item], max_bytes: int) -> Iterator[list[_Item]]:
    batch: list[_Item] = []
    size = 0
    for item in items:
        item_bytes = len(json.dumps(item[2], ensure_ascii=False).encode("utf-8")) + 200
        if batch and size + item_bytes > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_bytes
    if batch:
        yield batch


async def _send_once(client: httpx.AsyncClient, items: list[_Item]) -> list[_Item]:
    headers = {"Content-Type": "application/x-ndjson"}
    if settings.es_api_key:
        headers["Authorization"] = f"ApiKey {settings.es_api_key}"

    try:
        response = await client.post(
            f"{settings.es_url.rstrip('/')}/_bulk",
            content=_ndjson(items),
            headers=headers,
        )
    except httpx.RequestError as exc:
        logger.warning("관측 ES bulk 연결 실패(재시도 대상): error=%s", exc)
        return items

    if response.status_code in _RETRYABLE_STATUS:
        return items
    if response.status_code >= 400:
        logger.warning("관측 ES bulk 영구 실패(비재시도): status=%s", response.status_code)
        return []

    data = response.json()
    if not data.get("errors"):
        return []

    retryable: list[_Item] = []
    for result_item, sent in zip(data.get("items", []), items):
        result = next(iter(result_item.values()))
        status = result.get("status", 500)
        if status < 400:
            continue
        if status in _RETRYABLE_STATUS:
            retryable.append(sent)
        else:
            logger.warning(
                "관측 ES item 영구 실패(비재시도): status=%s, error=%s",
                status,
                result.get("error"),
            )
    return retryable


async def _send_with_retry(client: httpx.AsyncClient, batch: list[_Item]) -> None:
    pending = batch
    for attempt in range(settings.es_max_retries + 1):
        pending = await _send_once(client, pending)
        if not pending:
            return
        if attempt >= settings.es_max_retries:
            logger.warning("관측 ES 재시도 소진: %d건 미전송", len(pending))
            return
        delay = min(2**attempt, 30) * (0.5 + random.random())
        await asyncio.sleep(delay)


async def export(
    event_documents: list[dict[str, Any]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """event 문서를 Elasticsearch ``_bulk``로 보낸다(실패는 격리)."""

    if not settings.es_url:
        return
    items = _to_items(event_documents)
    if not items:
        return

    try:
        async with httpx.AsyncClient(
            timeout=settings.es_timeout_sec,
            transport=transport,
        ) as client:
            for batch in _chunks(items, _MAX_BULK_BYTES):
                await _send_with_retry(client, batch)
    except Exception as exc:  # noqa: BLE001 - 관측 전송은 주 처리를 깨지 않는다.
        logger.warning("관측 ES 전송 실패(격리): error=%s", exc)
