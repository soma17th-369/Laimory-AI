"""요청(task) 단위 관측 런타임 조립·flush."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.observability import ContentCapture, InMemoryObservationSink, Observer
from app.core.observability.documents import build_documents

logger = get_logger(__name__)


def build_task_observer() -> tuple[Observer | None, InMemoryObservationSink | None]:
    """관측 출력이 설정된 경우에만 task 전용 Observer 와 버퍼를 만든다."""

    should_collect = bool(settings.obs_local_dir) or bool(
        settings.obs_enabled and settings.es_url
    )
    if not should_collect:
        return None, None

    buffer = InMemoryObservationSink(max_events=settings.obs_max_events_per_task)
    observer = Observer(
        buffer,
        content_capture=ContentCapture(settings.obs_content_capture),
        max_payload_bytes=settings.obs_max_payload_bytes,
    )
    return observer, buffer


def _write_local(
    base_dir: str,
    task_id: str,
    task_document: dict[str, Any],
    event_documents: list[dict[str, Any]],
) -> None:
    """dev 검사용 task 요약과 event JSONL을 UTF-8로 저장한다."""

    out = Path(base_dir) / task_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "task.json").write_text(
        json.dumps(task_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (out / "events.jsonl").open("w", encoding="utf-8") as stream:
        for event_document in event_documents:
            stream.write(json.dumps(event_document, ensure_ascii=False) + "\n")


async def flush_task_observations(
    buffer: InMemoryObservationSink | None,
    *,
    task_id: str,
) -> None:
    """수집한 로그를 task/event 문서로 조립해 내보낸다. 모든 실패는 격리한다."""

    if buffer is None:
        return

    try:
        task_document, event_documents = build_documents(
            buffer.events,
            agent_version=settings.agent_version,
            dropped_event_count=buffer.dropped_count,
        )
        if task_document is None:
            return

        if settings.obs_local_dir:
            _write_local(
                settings.obs_local_dir,
                task_id,
                task_document,
                event_documents,
            )

        if settings.obs_enabled and settings.es_url:
            from app.core.observability.elasticsearch import export

            await export(task_document, event_documents)
    except Exception as exc:  # noqa: BLE001 - 관측은 주 처리를 깨지 않는다.
        logger.warning("관측 flush 실패(격리): taskId=%s, error=%s", task_id, exc)
