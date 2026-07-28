"""요청(task) 단위 관측 런타임 조립·flush."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger
from app.core.observability import ContentCapture, InMemoryObservationSink, Observer
from app.core.observability.documents import build_documents

logger = get_logger(__name__)


def build_task_observer() -> tuple[Observer | None, InMemoryObservationSink | None]:
    """관측 출력이 설정된 경우에만 task 전용 Observer와 버퍼를 만든다."""

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
    event_documents: list[dict[str, Any]],
) -> None:
    """dev 검사용 event JSONL을 UTF-8로 저장한다."""

    out = Path(base_dir) / task_id
    out.mkdir(parents=True, exist_ok=True)
    with (out / "events.jsonl").open("w", encoding="utf-8") as stream:
        for event_document in event_documents:
            stream.write(json.dumps(event_document, ensure_ascii=False) + "\n")


async def flush_task_observations(
    buffer: InMemoryObservationSink | None,
    *,
    task_id: str,
) -> None:
    """수집 로그를 event 문서로 조립해 내보낸다. 모든 실패는 격리한다."""

    if buffer is None:
        logger.info(
            "관측 수집 건너뜀: taskId=%s, obsEnabled=%s, "
            "esUrlConfigured=%s, localOutputConfigured=%s",
            task_id,
            settings.obs_enabled,
            bool(settings.es_url),
            bool(settings.obs_local_dir),
        )
        return

    try:
        event_documents = build_documents(
            buffer.events,
            agent_version=settings.agent_version,
            dropped_event_count=buffer.dropped_count,
        )
        if not event_documents:
            logger.warning("관측 flush 건너뜀: taskId=%s, reason=no_events", task_id)
            return

        if settings.obs_local_dir:
            _write_local(settings.obs_local_dir, task_id, event_documents)
            logger.info(
                "관측 로컬 저장 완료: taskId=%s, documents=%d",
                task_id,
                len(event_documents),
            )

        if settings.obs_enabled and settings.es_url:
            from app.core.observability.elasticsearch import export

            await export(event_documents, task_id=task_id)
        else:
            logger.info(
                "관측 ES 전송 건너뜀: taskId=%s, obsEnabled=%s, "
                "esUrlConfigured=%s, documents=%d",
                task_id,
                settings.obs_enabled,
                bool(settings.es_url),
                len(event_documents),
            )
    except Exception as exc:  # noqa: BLE001 - 관측은 주 처리를 깨지 않는다.
        # emit=False 다. 관측 flush 가 실패한 뒤라 관측으로는 아무것도 내보낼 수
        # 없다(버퍼는 이미 이 시점에 소비됐다). 로그가 유일한 통로다.
        report_error(
            logger,
            ErrorCode.OBSERVATION_EXPORT_FAILED,
            "관측 flush 실패(격리)",
            exc=exc,
            context={"taskId": task_id},
            emit=False,
        )
