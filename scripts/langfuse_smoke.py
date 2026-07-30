"""Langfuse 인증·수집 연결을 합성 데이터로 확인한다.

이 스크립트는 실제 Timeline 파이프라인이나 Agent 구조를 검증하지 않는다. 운영
trace와 혼동되지 않도록 전용 trace/observation 이름과 ``smoke``, ``synthetic``
태그를 사용한다.

실행:
    $env:LANGFUSE_ENABLED="true"
    $env:LANGFUSE_CONTENT_CAPTURE="NONE"
    uv run python -m scripts.langfuse_smoke
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from langfuse import propagate_attributes

from app.core.config import settings
from app.core.langfuse_tracing import (
    flush_langfuse,
    get_langfuse_client,
    trace_observation,
    update_observation,
)

TRACE_NAME = "langfuse-connectivity-smoke"
ROOT_NAME = "verify-langfuse-connectivity"
GENERATION_NAME = "verify-generation-usage"
TOOL_NAME = "verify-tool-observation"
SMOKE_TAGS = {"smoke", "synthetic"}
_SENSITIVE_SENTINEL = "smoke-user@example.com"


def _fetch_trace(client: Any, trace_id: str) -> Any:
    """비동기 ingestion이 반영될 때까지 trace를 짧게 재조회한다."""

    last_error: Exception | None = None
    for _ in range(30):
        try:
            return client.api.trace.get(trace_id)
        except Exception as exc:  # noqa: BLE001 - 마지막 실패에 원인을 연결한다.
            last_error = exc
            time.sleep(1)
    raise RuntimeError("Langfuse trace를 전송한 뒤 다시 조회하지 못했습니다.") from last_error


def _validate_trace_payload(payload: dict[str, Any]) -> set[str]:
    """연결 smoke의 이름·계층·타입·토큰·마스킹 계약을 검증한다."""

    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if _SENSITIVE_SENTINEL in serialized:
        raise RuntimeError("NONE 정책인데 합성 민감 본문이 trace에 노출됐습니다.")

    if payload.get("name") != TRACE_NAME:
        raise RuntimeError(
            f"합성 smoke trace 이름이 올바르지 않습니다: {payload.get('name')!r}"
        )
    if not SMOKE_TAGS.issubset(set(payload.get("tags") or [])):
        raise RuntimeError(
            f"합성 smoke 태그가 누락됐습니다: {payload.get('tags') or []}"
        )

    observations = payload.get("observations") or []
    names = {item.get("name") for item in observations}
    required = {ROOT_NAME, GENERATION_NAME, TOOL_NAME}
    if missing := required - names:
        raise RuntimeError(f"Langfuse 관측이 누락됐습니다: {sorted(missing)}")

    by_name = {item.get("name"): item for item in observations}
    root = by_name[ROOT_NAME]
    generation = by_name[GENERATION_NAME]
    tool = by_name[TOOL_NAME]
    if root.get("type") != "CHAIN":
        raise RuntimeError("연결 확인 root가 CHAIN으로 저장되지 않았습니다.")
    if generation.get("type") != "GENERATION" or tool.get("type") != "TOOL":
        raise RuntimeError("generation/tool observation type이 올바르지 않습니다.")
    if generation.get("parentObservationId") != root.get("id"):
        raise RuntimeError("generation이 연결 확인 root 아래에 중첩되지 않았습니다.")
    if tool.get("parentObservationId") != root.get("id"):
        raise RuntimeError("tool이 generation과 sibling으로 중첩되지 않았습니다.")

    usage = generation.get("usageDetails") or generation.get("usage") or {}
    if generation.get("model") != "synthetic-smoke-model":
        raise RuntimeError("generation model이 저장되지 않았습니다.")
    if usage.get("input") != 11 or usage.get("output") != 7:
        raise RuntimeError(f"generation token usage가 올바르지 않습니다: {usage}")
    return {name for name in names if isinstance(name, str)}


def main() -> None:
    if settings.langfuse_content_capture != "NONE":
        raise RuntimeError(
            "연결 smoke는 LANGFUSE_CONTENT_CAPTURE=NONE에서만 실행할 수 있습니다."
        )

    client = get_langfuse_client()
    if client is None:
        raise RuntimeError(
            "LANGFUSE_ENABLED=true와 LANGFUSE_PUBLIC_KEY/SECRET_KEY를 설정하세요."
        )
    if not client.auth_check():
        raise RuntimeError("Langfuse 인증에 실패했습니다.")

    smoke_id = f"langfuse-smoke-{uuid.uuid4()}"
    trace_id = client.create_trace_id(seed=smoke_id)

    with propagate_attributes(
        trace_name=TRACE_NAME,
        tags=sorted(SMOKE_TAGS),
        environment=settings.app_env,
        version=settings.agent_version,
        metadata={"synthetic": True, "smokeId": smoke_id},
    ):
        with trace_observation(
            ROOT_NAME,
            as_type="chain",
            input={"synthetic": True, "request": _SENSITIVE_SENTINEL},
            metadata={"synthetic": True, "purpose": "connectivity"},
            trace_context={"trace_id": trace_id},
        ) as root:
            with trace_observation(
                GENERATION_NAME,
                as_type="generation",
                input=[{"role": "user", "content": _SENSITIVE_SENTINEL}],
                model="synthetic-smoke-model",
                model_parameters={"temperature": 0.0},
                metadata={"provider": "synthetic", "synthetic": True},
            ) as generation:
                update_observation(
                    generation,
                    output=[{"role": "assistant", "content": "synthetic-result"}],
                    usage_details={"input": 11, "output": 7, "total": 18},
                )
            with trace_observation(
                TOOL_NAME,
                as_type="tool",
                input={"synthetic": True, "value": _SENSITIVE_SENTINEL},
            ) as tool:
                update_observation(tool, output={"ok": True, "synthetic": True})
            update_observation(root, output={"ok": True, "synthetic": True})

    flush_langfuse()
    trace = _fetch_trace(client, trace_id)
    payload = trace.model_dump(mode="json", by_alias=True)
    names = _validate_trace_payload(payload)

    print(
        json.dumps(
            {
                "auth": True,
                "synthetic": True,
                "traceId": trace_id,
                "traceName": TRACE_NAME,
                "traceUrl": client.get_trace_url(trace_id=trace_id),
                "observationNames": sorted(names),
                "contentPolicy": "NONE",
                "sensitiveContentExposed": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
