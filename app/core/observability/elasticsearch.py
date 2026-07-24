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
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_BULK_BYTES = 5 * 1024 * 1024
_Item = tuple[str, str, dict[str, Any]]


@dataclass(frozen=True)
class ExportResult:
    """한 번의 export에서 최종 확인된 문서 수."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _SendResult:
    """Bulk 한 번의 응답을 성공·영구 실패·재시도 대상으로 분류한 결과."""

    retryable: list[_Item]
    succeeded: int = 0
    failed: int = 0


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


async def _send_once(client: httpx.AsyncClient, items: list[_Item]) -> _SendResult:
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
        logger.warning(
            "관측 ES bulk 연결 실패(재시도 대상): documents=%d, error=%s",
            len(items),
            exc,
        )
        return _SendResult(retryable=items)

    if response.status_code in _RETRYABLE_STATUS:
        logger.warning(
            "관측 ES bulk 일시 실패(재시도 대상): status=%s, documents=%d",
            response.status_code,
            len(items),
        )
        return _SendResult(retryable=items)
    if response.status_code >= 400:
        logger.warning(
            "관측 ES bulk 영구 실패(비재시도): status=%s, documents=%d",
            response.status_code,
            len(items),
        )
        return _SendResult(retryable=[], failed=len(items))

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning(
            "관측 ES bulk 응답 JSON 파싱 실패(재시도 대상): documents=%d, error=%s",
            len(items),
            exc,
        )
        return _SendResult(retryable=items)

    if not data.get("errors"):
        return _SendResult(retryable=[], succeeded=len(items))

    retryable: list[_Item] = []
    succeeded = 0
    failed = 0
    result_items = data.get("items")
    if not isinstance(result_items, list):
        logger.warning(
            "관측 ES bulk 응답에 items가 없음(재시도 대상): documents=%d",
            len(items),
        )
        return _SendResult(retryable=items)

    for index, sent in enumerate(items):
        if index >= len(result_items):
            retryable.append(sent)
            continue

        result_item = result_items[index]
        if not isinstance(result_item, dict) or not result_item:
            retryable.append(sent)
            continue
        result = next(iter(result_item.values()))
        if not isinstance(result, dict):
            retryable.append(sent)
            continue
        status = result.get("status", 500)
        if status < 400:
            succeeded += 1
            continue
        if status in _RETRYABLE_STATUS:
            retryable.append(sent)
        else:
            failed += 1
            error = result.get("error")
            error_type = error.get("type") if isinstance(error, dict) else None
            logger.warning(
                "관측 ES item 영구 실패(비재시도): status=%s, errorType=%s",
                status,
                error_type,
            )
    return _SendResult(
        retryable=retryable,
        succeeded=succeeded,
        failed=failed,
    )


async def _send_with_retry(
    client: httpx.AsyncClient,
    batch: list[_Item],
) -> ExportResult:
    pending = batch
    succeeded = 0
    failed = 0
    for attempt in range(settings.es_max_retries + 1):
        result = await _send_once(client, pending)
        succeeded += result.succeeded
        failed += result.failed
        pending = result.retryable
        if not pending:
            return ExportResult(
                attempted=len(batch),
                succeeded=succeeded,
                failed=failed,
            )
        if attempt >= settings.es_max_retries:
            logger.warning("관측 ES 재시도 소진: %d건 미전송", len(pending))
            failed += len(pending)
            return ExportResult(
                attempted=len(batch),
                succeeded=succeeded,
                failed=failed,
            )
        delay = min(2**attempt, 30) * (0.5 + random.random())
        await asyncio.sleep(delay)

    return ExportResult(
        attempted=len(batch),
        succeeded=succeeded,
        failed=failed + len(pending),
    )


async def export(
    event_documents: list[dict[str, Any]],
    *,
    task_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ExportResult:
    """event 문서를 Elasticsearch ``_bulk``로 보내고 최종 건수를 반환한다."""

    if not settings.es_url:
        logger.warning(
            "관측 ES 전송 건너뜀: taskId=%s, reason=es_url_not_configured, documents=%d",
            task_id or "unknown",
            len(event_documents),
        )
        return ExportResult()

    items = _to_items(event_documents)
    if not items:
        logger.info(
            "관측 ES 전송 건너뜀: taskId=%s, reason=no_documents",
            task_id or "unknown",
        )
        return ExportResult()

    task_ids = {item[2]["taskId"] for item in items}
    task_label = task_id or (next(iter(task_ids)) if len(task_ids) == 1 else "multiple")
    batches = list(_chunks(items, _MAX_BULK_BYTES))
    succeeded = 0
    failed = 0

    logger.info(
        "관측 ES 전송 시작: taskId=%s, documents=%d, batches=%d, indexBase=%s",
        task_label,
        len(items),
        len(batches),
        settings.es_event_index,
    )

    try:
        async with httpx.AsyncClient(
            timeout=settings.es_timeout_sec,
            transport=transport,
        ) as client:
            for batch in batches:
                result = await _send_with_retry(client, batch)
                succeeded += result.succeeded
                failed += result.failed
    except Exception as exc:  # noqa: BLE001 - 관측 전송은 주 처리를 깨지 않는다.
        unclassified = len(items) - succeeded - failed
        failed += max(unclassified, 0)
        logger.warning(
            "관측 ES 전송 예외(격리): taskId=%s, errorType=%s, error=%s",
            task_label,
            type(exc).__name__,
            exc,
        )

    result = ExportResult(
        attempted=len(items),
        succeeded=succeeded,
        failed=failed,
    )
    log = logger.info if result.failed == 0 else logger.warning
    log(
        "관측 ES 전송 완료: taskId=%s, attempted=%d, succeeded=%d, failed=%d",
        task_label,
        result.attempted,
        result.succeeded,
        result.failed,
    )
    return result
