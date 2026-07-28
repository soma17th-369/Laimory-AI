"""실제 Elasticsearch 연결·템플릿·저장·조회 smoke 테스트.

기본 테스트 실행에서는 건너뛴다. SSH 또는 SSM 터널과 `.env`를 준비한 뒤
`LAIMORY_LIVE_ES=1`을 설정한 경우에만 실제 서버를 변경한다.
생성한 smoke 문서는 Kibana 확인에 사용할 수 있도록 삭제하지 않는다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from dotenv import load_dotenv

_ROOT_DIR = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = (
    _ROOT_DIR / "docs" / "observability" / "ai-timeline-task-index-template.json"
)
_LIVE_ES_ENABLED = os.getenv("LAIMORY_LIVE_ES", "").lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_es,
    pytest.mark.skipif(
        not _LIVE_ES_ENABLED,
        reason="LAIMORY_LIVE_ES=1 일 때만 실제 Elasticsearch를 호출합니다.",
    ),
]


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"ApiKey {api_key}"}


def _assert_success(response: httpx.Response, operation: str) -> None:
    assert response.status_code < 400, (
        f"{operation} 실패: HTTP {response.status_code}. "
        "ES URL·인증·보안 그룹·SSH/SSM 터널을 확인하세요."
    )


async def test_elasticsearch_live_template_export_and_search() -> None:
    """템플릿 설치 후 실제 exporter가 쓴 문서를 taskId로 다시 조회한다."""

    load_dotenv(_ROOT_DIR / ".env", override=False)

    from app.core.config import settings
    from app.core.observability import (
        InMemoryObservationSink,
        ObservationEvent,
        ObservationEventType,
        ObservationStage,
        Observer,
    )
    from app.core.observability.documents import build_documents
    from app.core.observability.elasticsearch import export

    assert settings.es_url, "LAIMORY_LIVE_ES=1이면 ES_URL 설정이 필요합니다."
    assert settings.es_event_index == "ai-timeline-task", (
        "live 템플릿과 동일하게 ES_EVENT_INDEX=ai-timeline-task를 사용하세요."
    )

    headers = _auth_headers(settings.es_api_key)
    template = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))

    task_id = f"es-smoke-{uuid4().hex[:12]}"
    marker = f"STORE-SANITIZED-{uuid4().hex}"
    now = datetime.now(timezone.utc)

    sink = InMemoryObservationSink()
    observer = Observer(sink)
    observer.emit(
        ObservationEvent(
            task_id=task_id,
            stage=ObservationStage.LLM,
            event_type=ObservationEventType.PROMPT,
            provider="live-smoke",
            model="connection-check",
            timestamp=now,
            payload={"prompt": marker, "imageCount": 0},
        )
    )
    observer.emit(
        ObservationEvent(
            task_id=task_id,
            stage=ObservationStage.FINAL,
            event_type=ObservationEventType.COMPLETED,
            timestamp=now,
            payload={"status": "SUCCESS"},
        )
    )
    documents = build_documents(sink.events, agent_version="live-smoke")
    index_name = f"{settings.es_event_index}-{now.strftime('%Y.%m')}"

    async with httpx.AsyncClient(timeout=settings.es_timeout_sec) as client:
        cluster = await client.get(settings.es_url.rstrip("/"), headers=headers)
        _assert_success(cluster, "클러스터 연결")

        installed = await client.put(
            f"{settings.es_url.rstrip('/')}/_index_template/ai-timeline-task",
            headers=headers,
            json=template,
        )
        _assert_success(installed, "인덱스 템플릿 설치")

        await export(documents)

        refreshed = await client.post(
            f"{settings.es_url.rstrip('/')}/{index_name}/_refresh",
            headers=headers,
        )
        _assert_success(refreshed, "인덱스 refresh")

        searched = await client.post(
            f"{settings.es_url.rstrip('/')}/{index_name}/_search",
            headers=headers,
            json={
                "query": {"term": {"taskId": task_id}},
                "sort": [{"sequence": "asc"}],
            },
        )
        _assert_success(searched, "smoke 문서 조회")

    hits = searched.json()["hits"]["hits"]
    assert len(hits) == 2
    assert [hit["_source"]["sequence"] for hit in hits] == [0, 1]
    serialized_hits = json.dumps(hits, ensure_ascii=False)
    assert marker in serialized_hits
    assert hits[0]["_source"]["payload"]["prompt"] == marker
    assert hits[-1]["_source"]["taskDurationMs"] >= 0

    print(f"[live-es] index={index_name} taskId={task_id}")
