"""User Memory 갱신 실행부 (#64).

`POST /v1/user-memory` 가 `taskId`, `taskToken`, 기존 `userMemory`, 확정된 `diaries`
를 받아 즉시 202 를 돌려준 뒤, 백그라운드에서

    1. 기존 프로필을 v1.0 계약으로 읽고(못 읽으면 없는 셈 치고),
    2. 하루 기록을 프롬프트에 실을 만큼으로 줄인 다음,
    3. 갱신 Agent 로 전체 갱신본을 만들고 크기·민감정보를 확정하고,
    4. **성공이든 실패든 결과 저장 API 를 정확히 한 번** 호출한다.

## 이 파일에서 가장 중요한 계약

**모든 실패 경로가 4번으로 수렴해야 한다.** 타임라인(#40)과 달리 완료 콜백이 없다.
결과 저장 호출이 곧 종료 통보이므로, 어느 경로에서든 그 호출을 빠뜨리면 App Server
작업은 아무 연락도 못 받고 TTL 까지 매달린다. 테스트가 "어떤 실패 경로에서도 정확히
1회" 를 고정한다.

## 순서 계약이 없다

호출이 하나라 지킬 순서가 없다. 토큰도 갱신되지 않는다 — 응답 body 로 새 값을 받을
기회 자체가 없기 때문이다. 접수 요청 body 의 값을 끝까지 쓴다.

## 실패 코드

    기존 프로필 계약 위반 → 1106  USER_MEMORY_CONTRACT_VIOLATION (흡수, 새로 만든다)
    구조화 출력 실패      → 1202  STRUCTURED_OUTPUT_INVALID
    LLM 호출 실패         → 1203  LLM_CALL_FAILED
    갱신본 생성 실패      → 1210  USER_MEMORY_GENERATION_FAILED (timeout 포함)
    크기·민감정보 위반    → 1304  USER_MEMORY_LIMIT_EXCEEDED
    결과 저장 실패        → 1305  USER_MEMORY_SUBMIT_FAILED (알릴 경로가 없다)

timeout 을 1201 이 아니라 1210 으로 보내는 것은 의도한 것이다. 1201 의 외부 메시지는
"타임라인 생성이 제한 시간을 초과했습니다" 라 이 경로에서는 틀린 말이 된다. timeout
이었는지는 운영 로그의 ``errorType`` 이 답한다.

## `SAVED` 전이와 분리된다

``FAILED`` 는 **"User Memory 가 안 바뀌었다"** 는 뜻이지 "하루 기록 저장이 실패했다"
가 아니다. ``DailyRecord`` 의 ``DRAFT → SAVED`` 전이는 앱 → App Server 구간에서 이미
끝나 있다. 둘을 묶으면 AI 실패가 사용자의 일기 저장을 되돌린다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from time import perf_counter

from pydantic import ValidationError

from app.agents.user_memory import UserMemoryAgent
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import code_of_or, report_error
from app.core.execution_context import ExecutionStage, execution_context
from app.core.inflight import track_inflight
from app.core.langfuse_tracing import (
    flush_langfuse,
    token_usage_scope,
    trace_user_memory_task,
    update_observation,
)
from app.core.logging import get_logger
from app.core.operational_logging import (
    EventOutcome,
    OperationalEvent,
    emit_event,
)
from app.schemas import TaskStatus
from app.schemas.user_memory import UserMemory
from app.schemas.user_memory_update import UserMemoryResultRequest, UserMemoryUpdateRequest
from app.services.app_server_client import AppServerClient, TaskToken
from app.services.user_memory_limits import build_diary_digest, serialized_chars
from app.services.user_memory_repair import UserMemoryOutcome, build_user_memory
from app.services.validator import resolve_timezone

logger = get_logger(__name__)


async def process_user_memory_task(
    task_id: str,
    client: AppServerClient,
    payload: UserMemoryUpdateRequest,
    agent: UserMemoryAgent | None = None,
) -> TaskStatus:
    """갱신본을 만들어 App Server 로 보내고 최종 상태를 돌려준다.

    반환값은 **작업 전체가 끝났는가**다. 갱신본을 만들었어도 결과 저장이 실패하면
    ``FAILED`` 다 — 아무것도 저장되지 않았고 App Server 는 그 사실조차 모른다.

    전체를 :func:`~app.core.inflight.track_inflight` 로 감싼다. HTTP 응답(202)은 이미
    나간 뒤이므로, ``GET /ping`` 이 유휴 여부를 물었을 때 여기가 아직 돌고 있다는
    사실을 알려야 컨테이너가 회수되지 않는다.
    """

    with track_inflight():
        token = TaskToken(payload.task_token)
        started = perf_counter()
        digest = build_diary_digest(payload.diaries)
        agent = agent or UserMemoryAgent()

        memory: UserMemory | None = None
        outcome: UserMemoryOutcome | None = None
        failure_code: ErrorCode | None = None

        with execution_context(task_id):
            try:
                with (
                    token_usage_scope() as token_usage,
                    trace_user_memory_task(task_id) as trace,
                ):
                    existing = _existing_memory(payload)
                    outcome = await asyncio.wait_for(
                        asyncio.to_thread(
                            build_user_memory,
                            agent,
                            existing,
                            digest,
                            updated_at=_updated_at(payload),
                        ),
                        timeout=settings.user_memory_timeout_sec,
                    )
                    memory = outcome.memory
                    update_observation(
                        trace,
                        output={
                            "status": TaskStatus.SUCCESS.value,
                            "repairAttempts": outcome.repair_attempts,
                            "durationMs": (perf_counter() - started) * 1000,
                            "tokenUsage": token_usage.summary(),
                            # 본문이 아니라 모양만 남긴다.
                            "userMemory": memory.trace_summary(),
                        },
                    )
            except asyncio.TimeoutError as exc:
                failure_code = report_error(
                    logger,
                    ErrorCode.USER_MEMORY_GENERATION_FAILED,
                    "User Memory 갱신 timeout",
                    exc=exc,
                    context={
                        "taskId": task_id,
                        "timeoutSec": settings.user_memory_timeout_sec,
                    },
                    stage=ExecutionStage.USER_MEMORY_AGENT,
                )
            except Exception as exc:  # noqa: BLE001 - 백그라운드 최종 방어선
                # 도메인 예외는 자기 코드를 갖고 있고(UserMemoryLimitError 1304,
                # StructuredOutputError 1202 …), 분류되지 않은 것만 1210 으로 떨어진다.
                failure_code = report_error(
                    logger,
                    code_of_or(exc, ErrorCode.USER_MEMORY_GENERATION_FAILED),
                    "User Memory 갱신 실패",
                    exc=exc,
                    context={"taskId": task_id},
                    stage=ExecutionStage.USER_MEMORY_AGENT,
                    exc_info=True,
                )

            result_sent = await _submit_result(
                client, task_id, token, memory, failure_code
            )
            status = (
                TaskStatus.SUCCESS
                if memory is not None and result_sent
                else TaskStatus.FAILED
            )

            if settings.langfuse_enabled:
                # 컨테이너가 작업 직후 회수돼도 trace 가 유실되지 않게 task 경계에서
                # 비운다. 동기 flush 는 event loop 를 막지 않도록 worker thread 에서.
                await asyncio.to_thread(flush_langfuse)

            _emit_completed(
                task_id,
                status=status,
                started=started,
                result_sent=result_sent,
                failure_code=failure_code,
                has_existing_memory=payload.user_memory is not None,
                digest_stats=digest.stats,
                outcome=outcome,
            )

    return status


def _existing_memory(payload: UserMemoryUpdateRequest) -> UserMemory | None:
    """기존 프로필을 읽는다. 계약 위반은 흡수하고 **새로 만든다**(1106).

    여기서 멈추면 그 사용자의 메모리는 영영 갱신되지 않는다 — 다음 날도 같은 값을
    읽고 같은 이유로 실패하기 때문이다. 읽지 못한 프로필은 이 서버의 유일한 소비자
    (Timeline·Question)도 못 읽으므로 이미 기능적으로 죽어 있고, 새로 만들면 며칠에
    걸쳐 다시 자란다.

    본문은 어디에도 남기지 않는다. 진단으로 남는 것은 **어떤 필드가 어떤 규칙에
    걸렸는지**뿐이다. 예외 객체를 :func:`report_error` 에 넘기지 않는 것은 실수가
    아니다 — ``str(ValidationError)`` 는 걸린 값을 ``input_value=...`` 로 인용한다.
    """

    try:
        return payload.parse_user_memory()
    except ValidationError as exc:
        errors = exc.errors()
        report_error(
            logger,
            ErrorCode.USER_MEMORY_CONTRACT_VIOLATION,
            "기존 userMemory 계약 위반으로 새로 생성합니다",
            context={
                "taskId": payload.task_id,
                "errorType": type(exc).__name__,
                "userMemoryErrorCount": len(errors),
                "userMemoryErrorFields": sorted(
                    {".".join(str(part) for part in error["loc"]) for error in errors}
                ),
                "userMemoryErrorTypes": sorted(
                    {str(error["type"]) for error in errors}
                ),
            },
        )
        return None


def _updated_at(payload: UserMemoryUpdateRequest) -> datetime:
    """갱신 시각. 사용자의 시간대로 남긴다.

    프로필을 읽는 사람도 사용자도 KST 로 산다. UTC 로 남기면 "언제 갱신됐나" 를 볼
    때마다 머릿속에서 9시간을 더해야 한다.
    """

    zones = [entry.record_time_zone for entry in payload.diaries if entry.record_time_zone]
    return datetime.now(resolve_timezone(zones[-1] if zones else None))


async def _submit_result(
    client: AppServerClient,
    task_id: str,
    token: TaskToken,
    memory: UserMemory | None,
    failure_code: ErrorCode | None,
) -> bool:
    """결과를 **정확히 한 번** 보낸다. 성공도 실패도 이 경로다.

    분기 기준은 ``memory`` 의 유무다(``failure_code`` 가 아니다). 실패 경로는 모두
    코드를 채우지만, 누군가 코드를 빠뜨려도 갱신본이 없으면 성공으로 통보되지
    않아야 한다.
    """

    request = (
        UserMemoryResultRequest.success(memory)
        if memory is not None
        else UserMemoryResultRequest.failure(
            failure_code or ErrorCode.USER_MEMORY_GENERATION_FAILED
        )
    )

    try:
        await client.submit_user_memory(task_id, token, request)
    except Exception as exc:  # noqa: BLE001 - 통보 실패가 작업을 깨뜨리지 않는다
        # 여기서 실패하면 App Server 에 알릴 다른 경로가 없다. 콜백을 없앤 계약의
        # 대가이며, 콜백이 있어도 401/404/409 에서는 같았으므로 회귀는 아니다.
        report_error(
            logger,
            code_of_or(exc, ErrorCode.USER_MEMORY_SUBMIT_FAILED),
            "User Memory 결과 저장 실패 — 통보할 다른 경로가 없습니다",
            exc=exc,
            context={"taskId": task_id, "status": request.status.value},
            stage=ExecutionStage.STORAGE,
            exc_info=True,
        )
        return False
    return True


def _emit_completed(
    task_id: str,
    *,
    status: TaskStatus,
    started: float,
    result_sent: bool,
    failure_code: ErrorCode | None,
    has_existing_memory: bool,
    digest_stats: dict[str, int],
    outcome: UserMemoryOutcome | None,
) -> None:
    """작업 하나를 운영 이벤트 한 건으로 닫는다. 본문은 싣지 않는다."""

    code = failure_code
    if code is None and not result_sent:
        # 갱신본은 만들었는데 통보가 실패한 경우다. 코드가 비면 이벤트만 보고는
        # 왜 실패로 잡혔는지 알 수 없다.
        code = ErrorCode.USER_MEMORY_SUBMIT_FAILED

    fields: dict[str, object] = {
        "taskId": task_id,
        "status": status.value,
        "durationMs": round((perf_counter() - started) * 1000, 3),
        "resultSent": result_sent,
        "errorCode": int(code) if code is not None else None,
        "hasExistingMemory": has_existing_memory,
        **digest_stats,
    }
    if outcome is not None:
        summary = outcome.memory.trace_summary()
        fields.update(
            repairAttempts=outcome.repair_attempts,
            schemaVersion=summary["schemaVersion"],
            filledFieldCount=summary["filledFieldCount"],
            customAttributeCount=summary["customAttributeCount"],
            serializedChars=serialized_chars(outcome.memory),
        )

    emit_event(
        OperationalEvent.USER_MEMORY_TASK_COMPLETED,
        outcome=(
            EventOutcome.SUCCESS
            if status is TaskStatus.SUCCESS
            else EventOutcome.FAILURE
        ),
        level=logging.INFO if status is TaskStatus.SUCCESS else logging.ERROR,
        **fields,
    )
