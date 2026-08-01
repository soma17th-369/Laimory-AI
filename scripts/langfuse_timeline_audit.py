"""일본 Langfuse 프로젝트에 비식별 실제 Timeline trace를 보내고 다시 감사한다.

운영 DB와 App Server에는 접근하지 않는다. 합성 source snapshot, 인메모리 저장소,
가짜 성공 콜백을 사용하되 Main Agent와 기본 Event/Timeline/Repair Agent 및 실제
설정 LLM provider는 그대로 실행한다.

실행:
    $env:LAIMORY_LANGFUSE_AUDIT="1"
    $env:UV_CACHE_DIR=".uv-cache"
    uv run python -m scripts.langfuse_timeline_audit
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

_AUDIT_FLAG = "LAIMORY_LANGFUSE_AUDIT"
_CALLBACK_SENTINEL = "audit-task-token-must-not-appear"
_VISIBLE_SENTINEL = "합성 프로젝트 회의"
_REQUIRED_AGENT_NAMES = {
    "main-agent",
    "event-agent-location",
    "event-agent-calendar",
    "event-agent-photo",
    "event-agent-sleep-activity",
    "event-agent-notification",
    "timeline-agent",
    "repair-agent",
}
_REQUIRED_BOUNDARY_NAMES = {
    "retrieve-source-snapshot",
    "normalize-source-snapshot",
    "merge-event-results",
    "store-timeline",
    "send-completion-callback",
    "finalize-timeline",
}
_REQUIRED_GENERATION_NAMES = {
    "infer-location-events",
    "infer-calendar-events",
    "infer-photo-events",
    "infer-sleep-activity-events",
    "infer-notification-events",
    "generate-timeline-draft",
    "analyze-timeline-repair",
}


def _prepare_environment() -> None:
    if os.getenv(_AUDIT_FLAG) != "1":
        raise RuntimeError(
            f"외부 trace 생성을 승인하려면 {_AUDIT_FLAG}=1을 설정하세요."
        )

    # 앱 모듈 import 전에 적용해야 SDK가 정확한 프로젝트·정책으로 초기화된다.
    os.environ["LANGFUSE_ENABLED"] = "true"
    os.environ["LANGFUSE_BASE_URL"] = "https://jp.cloud.langfuse.com"
    os.environ["LANGFUSE_SAMPLE_RATE"] = "1.0"
    os.environ["LANGFUSE_CONTENT_CAPTURE"] = "SANITIZED"
    os.environ["APP_ENV"] = "timeline-audit"
    os.environ["APP_SERVER_API_URL"] = "https://audit.invalid/s/api/v1"
    os.environ["PIPELINE_TIMEOUT_SEC"] = "300"
    # Agent Graph의 repair cycle을 실제 감사 trace에서도 확인할 수 있도록
    # 첫 분석이 수정을 요구하면 재분석까지 한 번 더 실행한다.
    os.environ["REPAIR_MAX_ITERATIONS"] = "2"


def _raw_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"laimory-langfuse-audit:{label}"))


def _synthetic_snapshot(task_id: str) -> Any:
    from app.schemas import (
        CollectedSnapshot,
        CollectedSourceItem,
        ItemType,
        TimelineWindow,
    )

    def source(
        label: str,
        item_type: ItemType,
        start: str,
        *,
        end: str | None = None,
        payload: dict[str, Any],
    ) -> CollectedSourceItem:
        return CollectedSourceItem(
            rawId=_raw_id(label),
            itemType=item_type,
            startAt=start,
            endAt=end,
            payload=payload,
        )

    return CollectedSnapshot(
        taskId=task_id,
        recordDate="2026-07-30T22:00:00+09:00",
        recordTimeZone="Asia/Seoul",
        timelineWindow=TimelineWindow(
            startTime="2026-07-30T00:00:00+09:00",
            endTime="2026-07-31T00:00:00+09:00",
        ),
        userMemory={
            "auditNotice": "이 입력은 Langfuse 구조 검수용 합성 데이터입니다.",
            "usualRoutine": "평일에는 합성 사무실에서 근무합니다.",
        },
        sourceItems=[
            source(
                "sleep",
                ItemType.HEALTH,
                "2026-07-30T00:10:00+09:00",
                end="2026-07-30T07:00:00+09:00",
                payload={"metric": "SLEEP", "durationMinutes": 410},
            ),
            source(
                "home",
                ItemType.STAY,
                "2026-07-30T07:00:00+09:00",
                end="2026-07-30T09:00:00+09:00",
                payload={
                    "latitude": 37.5001,
                    "longitude": 127.0001,
                    "address": "합성 주소 A",
                    "places": ["합성 자택"],
                    "durationText": "2시간",
                },
            ),
            source(
                "commute",
                ItemType.MOVEMENT,
                "2026-07-30T09:00:00+09:00",
                end="2026-07-30T09:30:00+09:00",
                payload={
                    "start": {"latitude": 37.5001, "longitude": 127.0001},
                    "end": {"latitude": 37.5101, "longitude": 127.0101},
                    "distanceMeters": 4200,
                    "transports": ["IN_VEHICLE"],
                },
            ),
            source(
                "office",
                ItemType.STAY,
                "2026-07-30T09:30:00+09:00",
                end="2026-07-30T18:00:00+09:00",
                payload={
                    "latitude": 37.5101,
                    "longitude": 127.0101,
                    "address": "합성 주소 B",
                    "places": ["합성 사무실"],
                    "durationText": "8시간 30분",
                },
            ),
            source(
                "calendar",
                ItemType.CALENDAR,
                "2026-07-30T10:00:00+09:00",
                end="2026-07-30T11:00:00+09:00",
                payload={
                    "title": _VISIBLE_SENTINEL,
                    "description": "합성 데이터로 진행하는 일정",
                    "locationText": "합성 회의실",
                    "allDay": False,
                },
            ),
            source(
                "photo",
                ItemType.PHOTO,
                "2026-07-30T12:20:00+09:00",
                payload={
                    # description 이 이미 있어 다운로드 대상이 아니다(#52).
                    "description": "합성 점심 식사 사진 설명",
                },
            ),
            source(
                "notification",
                ItemType.NOTIFICATION,
                "2026-07-30T17:40:00+09:00",
                payload={
                    "appName": "합성 메신저",
                    "title": "합성 퇴근 알림",
                    "text": "오늘 업무가 마무리되었습니다.",
                },
            ),
            source(
                "steps",
                ItemType.HEALTH,
                "2026-07-30T00:00:00+09:00",
                end="2026-07-30T23:59:00+09:00",
                payload={"metric": "STEPS", "value": 8450},
            ),
        ],
    )


async def _run_timeline(task_id: str) -> Any:
    """합성 입력으로 Timeline 전체를 한 번 돌린다.

    App Server 는 호출하지 않는다. 실제 서버로 나가는 유일한 통로가 클라이언트
    하나(이슈 #40)라 여기서만 인메모리 스텁으로 갈아 끼우면 된다.
    """

    from app.schemas import TaskStatus
    from app.services import timeline_runner
    from app.services.app_server_client import AppServerClient

    class AuditAppServerClient(AppServerClient):
        def __init__(self) -> None:
            self.draft = None
            self.result = None

        async def fetch_input(self, task_id: str, token: Any) -> Any:
            return _synthetic_snapshot(task_id)

        async def submit_result(self, task_id: str, token: Any, request: Any) -> None:
            self.result = request

        async def send_callback(self, task_id: str, token: Any, payload: Any) -> bool:
            return True

    app_server = AuditAppServerClient()

    # draft 원본은 결과 저장 계약으로 좁혀지기 전 값이라 runner 안에서만 보인다.
    # 감사용으로 한 벌 붙잡아 둔다.
    original_build = timeline_runner.build_result_request

    def capture(draft: Any) -> Any:
        app_server.draft = draft.model_copy(deep=True)
        return original_build(draft)

    timeline_runner.build_result_request = capture
    try:
        status = await timeline_runner.process_timeline_task(
            task_id,
            app_server,
            430000,
            "2026-07-30T00:00:00+09:00",
            "2026-07-31T00:00:00+09:00",
            _CALLBACK_SENTINEL,
        )
    finally:
        timeline_runner.build_result_request = original_build

    if status is not TaskStatus.SUCCESS:
        raise RuntimeError(f"합성 Timeline 실행이 실패했습니다: {status.value}")
    if app_server.draft is None:
        raise RuntimeError("Timeline 결과가 없습니다.")
    return app_server.draft


def _fetch_trace(client: Any, trace_id: str) -> dict[str, Any]:
    """비동기 ingestion이 필수 Timeline observation까지 반영될 때까지 기다린다."""

    last_error: Exception | None = None
    last_payload: dict[str, Any] | None = None
    required = _REQUIRED_AGENT_NAMES | _REQUIRED_BOUNDARY_NAMES
    # SDK flush 직후에도 Cloud ingestion 반영에는 짧은 지연이 있다. legacy trace
    # endpoint의 낮은 조회 한도를 소진하지 않도록 먼저 기다리고 적게 재시도한다.
    time.sleep(5)
    for _ in range(8):
        try:
            trace = client.api.trace.get(trace_id)
            last_payload = trace.model_dump(mode="json", by_alias=True)
            names = {
                item.get("name")
                for item in (last_payload.get("observations") or [])
            }
            if required <= names:
                return last_payload
        except Exception as exc:  # noqa: BLE001 - ingestion 반영을 재시도한다.
            last_error = exc
        time.sleep(2)
    if last_payload is not None:
        names = {
            item.get("name")
            for item in (last_payload.get("observations") or [])
        }
        raise RuntimeError(
            "Langfuse ingestion 대기 후에도 observation이 누락됐습니다: "
            f"{sorted(required - names)}"
        )
    raise RuntimeError("생성한 Langfuse trace를 다시 조회하지 못했습니다.") from last_error


def _audit_trace(
    payload: dict[str, Any],
    *,
    expected_task_id: str | None = None,
) -> dict[str, Any]:
    if payload.get("name") != "generate-timeline":
        raise RuntimeError(f"trace 이름이 잘못됐습니다: {payload.get('name')!r}")
    if payload.get("environment") != "timeline-audit":
        raise RuntimeError(
            f"audit environment가 분리되지 않았습니다: {payload.get('environment')!r}"
        )
    if "timeline" not in set(payload.get("tags") or []):
        raise RuntimeError(f"timeline 태그가 없습니다: {payload.get('tags') or []}")
    if (
        expected_task_id is not None
        and payload.get("sessionId") != expected_task_id
    ):
        raise RuntimeError(
            "taskId가 Langfuse sessionId로 전파되지 않았습니다: "
            f"{payload.get('sessionId')!r} != {expected_task_id!r}"
        )

    observations = payload.get("observations") or []
    by_name: dict[str, list[dict[str, Any]]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for item in observations:
        by_name.setdefault(item.get("name"), []).append(item)
        if item.get("id"):
            by_id[item["id"]] = item

    required = _REQUIRED_AGENT_NAMES | _REQUIRED_BOUNDARY_NAMES
    if missing := required - by_name.keys():
        raise RuntimeError(f"필수 observation이 누락됐습니다: {sorted(missing)}")

    for name in _REQUIRED_AGENT_NAMES:
        if any(item.get("type") != "AGENT" for item in by_name[name]):
            raise RuntimeError(f"{name}이 AGENT 타입이 아닙니다.")

    expected_types = {
        "generate-timeline": "SPAN",
        **{
            name: "CHAIN" if name == "merge-event-results" else "SPAN"
            for name in _REQUIRED_BOUNDARY_NAMES
        },
    }
    for name, expected_type in expected_types.items():
        if any(item.get("type") != expected_type for item in by_name[name]):
            raise RuntimeError(f"{name}의 타입이 {expected_type}이 아닙니다.")

    root = by_name["generate-timeline"][0]
    main_agent = by_name["main-agent"][0]
    if main_agent.get("parentObservationId") != root.get("id"):
        raise RuntimeError("Main Agent가 Timeline root 아래에 중첩되지 않았습니다.")
    for name in (
        "event-agent-location",
        "event-agent-calendar",
        "event-agent-photo",
        "event-agent-sleep-activity",
        "event-agent-notification",
        "merge-event-results",
        "timeline-agent",
        "repair-agent",
    ):
        if any(
            item.get("parentObservationId") != main_agent.get("id")
            for item in by_name[name]
        ):
            raise RuntimeError(f"{name}이 Main Agent 아래에 중첩되지 않았습니다.")

    generations = [
        item for item in observations if item.get("type") == "GENERATION"
    ]
    if not generations:
        raise RuntimeError("generation observation이 한 건도 없습니다.")
    generation_names = {item.get("name") for item in generations}
    if "call-llm" in generation_names:
        raise RuntimeError("stage를 잃은 범용 call-llm generation이 남아 있습니다.")
    if missing := _REQUIRED_GENERATION_NAMES - generation_names:
        raise RuntimeError(f"구체적인 generation 이름이 누락됐습니다: {sorted(missing)}")
    for generation in generations:
        if not generation.get("model"):
            raise RuntimeError(f"{generation.get('name')}의 model이 없습니다.")
        usage = generation.get("usageDetails") or {}
        usage_buckets = {
            key: value
            for key, value in usage.items()
            if "input" in key or "output" in key
        }
        if not usage_buckets or not all(
            isinstance(value, (int, float)) and value >= 0
            for value in usage_buckets.values()
        ):
            raise RuntimeError(
                f"{generation.get('name')}의 실제 token usage 필드가 없습니다."
            )
        parent = by_id.get(generation.get("parentObservationId"))
        if parent is None or parent.get("type") not in {
            "AGENT",
            "CHAIN",
            "SPAN",
        }:
            raise RuntimeError(
                f"{generation.get('name')}의 Agent/span 부모가 올바르지 않습니다."
            )

    graph_nodes = {
        item.get("name")
        for item in observations
        if item.get("type") not in {"SPAN", "EVENT", "GENERATION"}
    }
    allowed_graph_nodes = _REQUIRED_AGENT_NAMES | {
        "merge-event-results",
        "analyze-repair-iteration",
        "execute-repair-plan",
        "confirm-repair-iteration",
    }
    if unexpected := graph_nodes - allowed_graph_nodes:
        raise RuntimeError(
            "Agent Graph에 계약에 없는 구조 노드가 남았습니다: "
            f"{sorted(unexpected)}"
        )

    for name in required:
        for item in by_name[name]:
            if item.get("input") is None or item.get("output") is None:
                raise RuntimeError(f"{name}의 input/output이 비어 있습니다.")

    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if _VISIBLE_SENTINEL not in serialized:
        raise RuntimeError("SANITIZED 본문이 trace에서 확인되지 않습니다.")
    if _CALLBACK_SENTINEL in serialized:
        raise RuntimeError("callback token이 trace에 노출됐습니다.")
    for secret_key in (
        "secret_key",
        "secretkey",
        "aws_secret_access_key",
        "authorization",
    ):
        if secret_key in serialized.lower():
            raise RuntimeError(f"민감 키 이름이 trace에 노출됐습니다: {secret_key}")

    root_output = root.get("output") or {}
    token_usage = root_output.get("tokenUsage") or {}
    if token_usage.get("generationCount") != len(generations):
        raise RuntimeError(
            "root generationCount가 실제 generation 수와 다릅니다: "
            f"{token_usage.get('generationCount')} != {len(generations)}"
        )
    if not isinstance(token_usage.get("totalTokens"), int) or token_usage[
        "totalTokens"
    ] <= 0:
        raise RuntimeError("root token 합계가 없습니다.")
    if not isinstance(root_output.get("durationMs"), (int, float)):
        raise RuntimeError("root durationMs가 없습니다.")

    type_counts: dict[str, int] = {}
    for item in observations:
        observation_type = str(item.get("type"))
        type_counts[observation_type] = type_counts.get(observation_type, 0) + 1

    return {
        "observationCount": len(observations),
        "observationTypeCounts": dict(sorted(type_counts.items())),
        "agentNames": sorted(
            name
            for name, items in by_name.items()
            if any(item.get("type") == "AGENT" for item in items)
        ),
        "generationNames": sorted(
            {item.get("name") for item in generations if item.get("name")}
        ),
        "generationCount": len(generations),
        "tokenUsage": token_usage,
        "durationMs": root_output["durationMs"],
        "contentVisible": True,
        "callbackTokenExposed": False,
    }


def main() -> None:
    _prepare_environment()

    from app.core.config import settings
    from app.core.langfuse_tracing import get_langfuse_client

    if not (
        settings.langfuse_enabled
        and settings.langfuse_public_key
        and settings.langfuse_secret_key.get_secret_value()
    ):
        raise RuntimeError("일본 Langfuse project key pair가 설정되지 않았습니다.")
    if settings.langfuse_base_url != "https://jp.cloud.langfuse.com":
        raise RuntimeError(f"일본 리전이 아닙니다: {settings.langfuse_base_url}")
    if settings.langfuse_content_capture != "SANITIZED":
        raise RuntimeError("Timeline audit에는 SANITIZED 정책이 필요합니다.")
    if settings.bedrock_aws_profile:
        # APP_ENV는 Langfuse 화면 분리를 위해 local이 아니므로 BedrockProvider의
        # local 전용 분기 대신 boto3 기본 체인이 같은 공유 프로필을 읽게 한다.
        os.environ["AWS_PROFILE"] = settings.bedrock_aws_profile

    existing_task_id = os.getenv("LAIMORY_LANGFUSE_AUDIT_TASK_ID", "").strip()
    task_id = existing_task_id or f"langfuse-audit-{uuid.uuid4()}"
    draft = None if existing_task_id else asyncio.run(_run_timeline(task_id))
    client = get_langfuse_client()
    if client is None or not client.auth_check():
        raise RuntimeError("Langfuse 인증을 확인하지 못했습니다.")

    trace_id = client.create_trace_id(seed=task_id)
    payload = _fetch_trace(client, trace_id)
    audit = _audit_trace(payload, expected_task_id=task_id)
    print(
        "LANGFUSE_AUDIT_RESULT="
        + json.dumps(
            {
                "ok": True,
                "synthetic": True,
                "taskId": task_id,
                "traceId": trace_id,
                "traceUrl": client.get_trace_url(trace_id=trace_id),
                "timelineEventCount": len(draft.events) if draft is not None else None,
                "reusedExistingTrace": bool(existing_task_id),
                **audit,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
