"""타임라인 초안 처리 실행부.

`POST /v1/timeline` 이 `taskId`, `taskToken`, `dailyRecordId`, `window` 를 받아
즉시 응답을 돌려준 뒤, 백그라운드에서

    1. App Server 입력 조회 API 로 `taskId` 의 수집 원본을 읽어오고,
    2. 요청이 준 `window` 를 스냅샷에 정본으로 덮어쓴 뒤 `normalize` 로 도메인별로
       분리·정규화하고,
    3. 메인 에이전트를 실행한 다음,
    4. 확정 결과를 App Server 결과 저장 API 로 보내고,
    5. **저장 200 을 확인한 뒤에만** SUCCESS 를 콜백한다.

AI 서버는 task 상태도, 서비스 데이터도 직접 보관하지 않는다(이슈 #40). staging DB
접근은 전부 App Server API 로 대체됐고, 콜백은 SUCCESS/FAILED 통보만 한다.

토큰은 작업 하나에 하나다. 최초 값은 접수 요청 body 로 들어오고 그 뒤 응답 body 로
갱신되며(`TaskToken`), 모든 요청이 같은 홀더의 최신 값을 `Task-Token` 헤더로
보낸다. 토큰 값은 로그·관측 어디에도 남기지 않는다 — 갱신 횟수만 남긴다.

전체 메인 에이전트는 `pipeline_timeout_sec` 로 감싼다. timeout 이나 예기치 못한
오류가 나면 FAILED 로 콜백하고, 결과는 저장하지 않는다.

실패는 **원인별 정수 코드**(`app.core.error_codes`)로 식별한다. 콜백·관측 이벤트·
운영 로그가 모두 같은 코드 하나를 쓴다.

    입력 없음          → 1101  SOURCE_SNAPSHOT_NOT_FOUND   (입력 조회 404)
    원본 계약 위반     → 1102  SOURCE_CONTRACT_VIOLATION   (SourceBatchError)
    입력 조회 실패     → 1105  SOURCE_FETCH_FAILED         (5xx/timeout 소진)
    파이프라인 timeout → 1201  PIPELINE_TIMEOUT
    구조화 출력 실패   → 1202  STRUCTURED_OUTPUT_INVALID   (StructuredOutputError)
    저장 검증 실패     → 1301  TIMELINE_STORAGE_VALIDATION_FAILED
    결과 저장 실패     → 1303  TIMELINE_RESULT_SUBMIT_FAILED
    토큰 거절(401)     → 1404  APP_SERVER_UNAUTHORIZED     (콜백 없이 중단)
    task 없음(404)     → 1405  APP_SERVER_TASK_NOT_FOUND   (콜백 없이 중단)
    순서 충돌(409)     → 1406  APP_SERVER_CONFLICT         (콜백 없이 중단)
    그 밖              → 1901  INTERNAL_ERROR

401/404/409 는 콜백도 같은 이유로 거절되므로 통보하지 않고 멈춘다. 그 밖의 실패는
FAILED 콜백을 보낸다 — 결과 저장이 재시도까지 실패한 경우도 여기 속한다(저장에
성공한 뒤에는 어떤 이유로도 FAILED 를 보내지 않는다).

콜백 `error` 에는 코드에 묶인 안전 메시지만 싣는다. 원본 예외 메시지는 로그에만
남는다 — 콜백 본문은 AI 서버 밖으로 나가므로 내부 식별자·경로가 실리면 안 된다.

처리 전 구간은 task 단위 실행 컨텍스트(`execution_context`)로 감싼다. 상관키는
`taskId` 하나이며, `contextvars` 로 열려 있어 `asyncio.to_thread` 로 도는 Event/
Timeline/Repair Agent 와 그 안의 LLM 호출까지 같은 taskId 로 이어진다. 이 컨텍스트가
모든 운영 로그 줄에 `taskId`/`stage` 를 붙이고, Langfuse generation 이름도 여기서
정해진다.
"""

import asyncio
from dataclasses import dataclass
from time import perf_counter

from app.agents.main import run_main_agent
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import code_of, report_error
from app.core.inflight import track_inflight
from app.core.langfuse_tracing import (
    flush_langfuse,
    token_usage_scope,
    trace_observation,
    trace_timeline_task,
    update_observation,
)
from app.core.execution_context import (
    ExecutionStage,
    execution_context,
    execution_scope,
)
from app.core.logging import get_logger, log_fields, stage_span
from app.schemas import (
    TaskStatus,
    TimelineCallbackPayload,
    TimelineDraft,
    TimelineDraftRequest,
)
from app.schemas.source_snapshot import TimelineWindow
from app.services.app_server_client import AppServerClient, AppServerError, TaskToken
from app.services.normalizer import normalize
from app.services.source_contract import source_raw_ids
from app.services.timeline_result import build_result_request
from app.services.timeline_validator import (
    TimelineValidationError,
    ensure_timeline_valid_for_storage,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class _TimelineRunResult:
    status: TaskStatus
    request: TimelineDraftRequest | None
    draft: TimelineDraft | None
    failure_code: ErrorCode | None
    callback_sent: bool


async def process_timeline_task(
    task_id: str,
    client: AppServerClient,
    daily_record_id: int,
    window_start: str,
    window_end: str,
    task_token: str,
) -> TaskStatus:
    """입력을 조회해 정규화·실행·저장하고 성공/실패를 콜백한다.

    AI 서버는 상태를 보관하지 않으므로(App Server 소유), 결과는 콜백으로만 통보하고
    최종 상태를 반환값으로 돌려준다(호출부/테스트 관찰용).

    전체를 `track_inflight` 로 감싼다. HTTP 응답(202)은 이미 나간 뒤이므로,
    AgentCore Runtime 이 `GET /ping` 으로 유휴 여부를 물었을 때 여기가 아직 돌고
    있다는 사실을 알려야 컨테이너가 회수되지 않는다.
    """

    with track_inflight():
        # 작업 전체가 이 홀더 하나를 공유한다. 응답 body 로 새 토큰이 오면 여기서
        # 갈아 끼워지고, 다음 요청부터 갱신된 값이 헤더로 나간다.
        token = TaskToken(task_token)
        started = perf_counter()
        try:
            with (
                token_usage_scope() as token_usage,
                trace_timeline_task(
                    task_id,
                    daily_record_id=daily_record_id,
                    window_start=window_start,
                    window_end=window_end,
                ) as langfuse_trace,
            ):
                # 이 컨텍스트가 taskId 를 이후 모든 운영 로그와 Langfuse
                # generation 이름에 실어 나른다.
                with execution_context(task_id):
                    result = await _process_observed(
                        task_id,
                        client,
                        token,
                        daily_record_id,
                        window_start,
                        window_end,
                    )
                status = result.status
                root_input: dict[str, object] = {
                    "taskId": task_id,
                    "dailyRecordId": daily_record_id,
                    "window": {"start": window_start, "end": window_end},
                }
                if result.request is not None:
                    root_input["request"] = result.request.model_dump(
                        by_alias=True,
                        mode="json",
                    )
                root_output: dict[str, object] = {
                    "status": status.value,
                    "persisted": status is TaskStatus.SUCCESS,
                    "callbackSent": result.callback_sent,
                    # 토큰 값은 남기지 않는다. 몇 번 갈렸는지만 본다.
                    "tokenRefreshCount": token.refresh_count,
                    "durationMs": (perf_counter() - started) * 1000,
                    "tokenUsage": token_usage.summary(),
                }
                if result.draft is not None:
                    root_output["timeline"] = result.draft.model_dump(
                        by_alias=True,
                        mode="json",
                    )
                if result.failure_code is not None:
                    root_output["errorCode"] = int(result.failure_code)

                with trace_observation(
                    "finalize-timeline",
                    as_type="span",
                    input={
                        "status": status.value,
                        **(
                            {"errorCode": int(result.failure_code)}
                            if result.failure_code is not None
                            else {}
                        ),
                    },
                    metadata={"stage": ExecutionStage.FINAL.value},
                ) as final_observation:
                    update_observation(
                        final_observation,
                        output=root_output,
                        level=(
                            "DEFAULT"
                            if status is TaskStatus.SUCCESS
                            else "ERROR"
                        ),
                    )

                update_observation(
                    langfuse_trace,
                    input=root_input,
                    output=root_output,
                    level="DEFAULT" if status is TaskStatus.SUCCESS else "ERROR",
                    status_message=(
                        None
                        if status is TaskStatus.SUCCESS
                        else "Timeline 처리가 실패했습니다."
                    ),
                )
        finally:
            # AgentCore/EC2 컨테이너가 작업 직후 회수돼도 trace가 유실되지 않도록
            # 비동기 worker queue를 task 경계에서 비운다. 동기 flush는 event loop를
            # 막지 않도록 worker thread에서 실행한다.
            if settings.langfuse_enabled:
                await asyncio.to_thread(flush_langfuse)

    return status


async def _process_observed(
    task_id: str,
    client: AppServerClient,
    token: TaskToken,
    daily_record_id: int,
    window_start: str,
    window_end: str,
) -> _TimelineRunResult:
    """실행 컨텍스트가 열린 상태에서 실제 처리·저장·콜백을 수행한다.

    단계 경계를 운영 로그로 남긴다: REQUEST(입력 조회·정규화) → (MAIN_AGENT 이하는
    하위에서) → STORAGE(결과 저장 API) → CALLBACK(콜백) → FINAL(성공/실패/timeout).
    남기는 값은 건수·소요시간·errorCode 뿐이고, 실행 본문은 Langfuse 로만 나간다.
    """

    status = TaskStatus.SUCCESS
    # 실패 원인을 식별하는 카탈로그 코드. 성공이면 None 이다. 콜백·로그·Langfuse 가
    # 모두 이 하나를 쓰므로 세 곳이 다른 값을 말할 수 없다.
    failure_code: ErrorCode | None = None
    # 실패는 stage_span 밖(아래 except)에서 기록하므로 그때는 실행 컨텍스트가 이미
    # 닫혀 있다. 어느 단계에서 깨졌는지는 이 값으로 명시해 넘긴다.
    active_stage = ExecutionStage.REQUEST
    request: TimelineDraftRequest | None = None
    draft: TimelineDraft | None = None
    # 401/404/409 는 콜백도 같은 이유로 거절된다. 보내 봐야 실패 로그만 하나 더
    # 남으므로 통보를 포기한다.
    abort_callback = False

    try:
        with stage_span(
            logger,
            ExecutionStage.REQUEST,
            dailyRecordId=daily_record_id,
        ) as request_outcome:
            retrieval_started = perf_counter()
            with trace_observation(
                "retrieve-source-snapshot",
                as_type="span",
                input={"taskId": task_id},
                metadata={"client": type(client).__name__, "source": "app-server-api"},
            ) as retrieval:
                try:
                    snapshot = await client.fetch_input(task_id, token)
                except Exception as exc:
                    update_observation(
                        retrieval,
                        output={
                            "found": False,
                            "errorCode": int(code_of(exc)),
                            "durationMs": (
                                perf_counter() - retrieval_started
                            )
                            * 1000,
                        },
                        level="ERROR",
                        status_message="수집 원본 조회에 실패했습니다.",
                    )
                    raise
                update_observation(
                    retrieval,
                    output={
                        "found": True,
                        "durationMs": (perf_counter() - retrieval_started) * 1000,
                        "sourceItemCount": len(snapshot.source_items),
                        "snapshot": snapshot.model_dump(by_alias=True, mode="json"),
                    },
                )

            # 요청이 준 window 를 정본으로 덮어쓴다. 입력 조회 응답에도 window 가 있지만
            # 접수 요청이 정본이라는 규칙을 유지한다(두 값은 같아야 정상이다).
            normalize_started = perf_counter()
            with trace_observation(
                "normalize-source-snapshot",
                as_type="span",
                input={
                    "snapshot": snapshot.model_dump(by_alias=True, mode="json"),
                    "window": {"start": window_start, "end": window_end},
                },
            ) as normalization:
                try:
                    snapshot = snapshot.model_copy(
                        update={
                            "timeline_window": TimelineWindow(
                                start_time=window_start,
                                end_time=window_end,
                            )
                        }
                    )
                    request = normalize(snapshot)
                except Exception as exc:
                    update_observation(
                        normalization,
                        output={
                            "errorCode": int(code_of(exc)),
                            "durationMs": (
                                perf_counter() - normalize_started
                            )
                            * 1000,
                        },
                        level="ERROR",
                        status_message="수집 스냅샷 정규화에 실패했습니다.",
                    )
                    raise
                update_observation(
                    normalization,
                    output={
                        "durationMs": (perf_counter() - normalize_started) * 1000,
                        "request": request.model_dump(by_alias=True, mode="json"),
                    },
                )
            request_outcome["sourceItemCount"] = len(snapshot.source_items)
            request_outcome["inputItemCounts"] = request.source_item_counts()
            request_outcome["hasUserMemory"] = request.user_memory is not None

        active_stage = ExecutionStage.MAIN_AGENT
        draft = await asyncio.wait_for(
            run_main_agent(request),
            timeout=settings.pipeline_timeout_sec,
        )

        # 확정 결과를 App Server 결과 저장 API 로 보낸다. 저장/검증 실패는 아래
        # except 로 잡혀 FAILED 로 처리된다.
        active_stage = ExecutionStage.STORAGE
        with stage_span(
            logger,
            ExecutionStage.STORAGE,
            dailyRecordId=daily_record_id,
            eventCount=len(draft.events),
        ):
            storage_started = perf_counter()
            with trace_observation(
                "store-timeline",
                as_type="span",
                input={
                    "taskId": task_id,
                    "dailyRecordId": daily_record_id,
                    "timeline": draft.model_dump(by_alias=True, mode="json"),
                },
                metadata={
                    "client": type(client).__name__,
                    "target": "app-server-api",
                },
            ) as storage:
                try:
                    # 입력에 없는 rawId 가 결과에 남아 있으면 저장 요청을 보내지 않는다.
                    # App Server 가 거절할 참조를 굳이 왕복시킬 이유가 없다.
                    ensure_timeline_valid_for_storage(draft, source_raw_ids(snapshot))
                    result_request = build_result_request(draft)
                    await client.submit_result(task_id, token, result_request)
                except Exception as exc:
                    update_observation(
                        storage,
                        output={
                            "saved": False,
                            "errorCode": int(code_of(exc)),
                            "durationMs": (
                                perf_counter() - storage_started
                            )
                            * 1000,
                            "timeline": draft.model_dump(
                                by_alias=True,
                                mode="json",
                            ),
                        },
                        level="ERROR",
                        status_message="Timeline 결과 저장에 실패했습니다.",
                    )
                    raise
                update_observation(
                    storage,
                    output={
                        "saved": True,
                        "eventCount": len(draft.events),
                        "durationMs": (perf_counter() - storage_started) * 1000,
                        "timeline": draft.model_dump(by_alias=True, mode="json"),
                    },
                )
    except asyncio.TimeoutError as exc:
        status = TaskStatus.FAILED
        failure_code = report_error(
            logger,
            ErrorCode.PIPELINE_TIMEOUT,
            "타임라인 처리 timeout",
            exc=exc,
            context={
                "taskId": task_id,
                "timeoutSec": settings.pipeline_timeout_sec,
            },
            stage=ExecutionStage.MAIN_AGENT,
        )
    except Exception as exc:  # noqa: BLE001 - 백그라운드 최종 방어선
        status = TaskStatus.FAILED
        # 도메인 예외는 자기 코드를 갖고 있고(SourceBatchError 1102,
        # AppServerError 1105/1303/1404…, TimelineValidationError 1301,
        # StructuredOutputError 1202 …), 분류되지 않은 예외만 INTERNAL_ERROR 로
        # 떨어진다.
        abort_callback = isinstance(exc, AppServerError) and exc.abort
        failure_context: dict[str, object] = {"taskId": task_id}
        if isinstance(exc, TimelineValidationError):
            failure_context["violationCodes"] = exc.violation_codes
            failure_context["violationCount"] = len(exc.details)
        if abort_callback:
            failure_context["callbackSkipped"] = True
        failure_code = report_error(
            logger,
            code_of(exc),
            "타임라인 처리 실패",
            exc=exc,
            context=failure_context,
            stage=active_stage,
            exc_info=True,
        )

    callback_sent = False
    if abort_callback:
        # 여기까지 오면 실패 코드는 위에서 이미 로그에 남았다. 콜백을 건너뛴
        # 사실만 한 줄 더 남긴다.
        logger.warning(
            "완료 콜백 생략: App Server 가 토큰/task/순서를 거절해 통보 대상이 없습니다.",
            extra=log_fields(
                errorCode=int(failure_code) if failure_code is not None else None,
                taskId=task_id,
                stage=ExecutionStage.CALLBACK.value,
            ),
        )
    else:
        callback_sent = await _send_completion_callback(
            client,
            task_id,
            token,
            status,
            failure_code,
        )

    logger.info(
        "타임라인 처리 종료",
        extra=log_fields(
            stage=ExecutionStage.FINAL.value,
            status=status.value,
            callbackSent=callback_sent,
            errorCode=int(failure_code) if failure_code is not None else None,
        ),
    )
    return _TimelineRunResult(
        status=status,
        request=request,
        draft=draft,
        failure_code=failure_code,
        callback_sent=callback_sent,
    )


async def _send_completion_callback(
    client: AppServerClient,
    task_id: str,
    token: TaskToken,
    status: TaskStatus,
    failure_code: ErrorCode | None,
) -> bool:
    """성공/실패 통보를 보낸다. 저장이 끝난 뒤 주 결과를 알리는 마지막 단계다."""

    # 실패면 원인별 코드와 그 코드에 묶인 안전 메시지가 나간다. 원본 예외
    # 메시지는 위에서 로그에만 남겼다.
    #
    # 분기 기준은 `status` 다(`failure_code` 가 아니다). 실패 경로는 모두 코드를
    # 채우지만, 누군가 status 만 FAILED 로 두고 코드를 빠뜨리면 코드 기준 분기는
    # 성공 콜백을 보내 버린다. 실패를 성공으로 통보하는 것보다 코드가 뭉개지는
    # 편이 낫다.
    payload = (
        TimelineCallbackPayload.success()
        if status is TaskStatus.SUCCESS
        else TimelineCallbackPayload.failure(failure_code or ErrorCode.INTERNAL_ERROR)
    )
    callback_started = perf_counter()
    callback_body = payload.model_dump(by_alias=True, mode="json")
    with (
        execution_scope(ExecutionStage.CALLBACK),
        trace_observation(
            "send-completion-callback",
            as_type="span",
            input={
                "taskId": task_id,
                "status": status.value,
                "payload": callback_body,
            },
            metadata={"target": "app-server-api"},
        ) as callback_observation,
    ):
        sent = await client.send_callback(task_id, token, payload)
        update_observation(
            callback_observation,
            output={
                "sent": sent,
                "status": status.value,
                "durationMs": (perf_counter() - callback_started) * 1000,
                "payload": callback_body,
                **(
                    {"errorCode": int(ErrorCode.CALLBACK_SEND_FAILED)}
                    if not sent
                    else {}
                ),
            },
            level="DEFAULT" if sent else "ERROR",
            status_message=None if sent else "완료 콜백 전송에 실패했습니다.",
        )

    if sent:
        logger.info(
            "완료 콜백 전송 완료",
            extra=log_fields(
                stage=ExecutionStage.CALLBACK.value,
                taskId=task_id,
                status=status.value,
                durationMs=round((perf_counter() - callback_started) * 1000, 3),
            ),
        )
    else:
        # 콜백 전송 실패는 Timeline 결과를 바꾸지 않는다(이미 저장은 끝났다).
        # 다만 App Server 가 결과를 못 받은 상태이므로 코드로 남겨 추적한다.
        report_error(
            logger,
            ErrorCode.CALLBACK_SEND_FAILED,
            "완료 콜백 전송 실패",
            context={"taskId": task_id, "status": status.value},
            stage=ExecutionStage.CALLBACK,
        )
    return sent
