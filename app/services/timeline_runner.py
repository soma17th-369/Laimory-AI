"""타임라인 초안 처리 실행부.

`POST /v1/timeline` 이 `taskId`, `callbackToken`, `dailyRecordId`, `window` 를 받아
즉시 응답을 돌려준 뒤, 백그라운드에서

    1. 저장소(`SourceRepository`)에서 `taskId` 로 수집 스냅샷을 읽어오고,
    2. 요청이 준 `window` 를 스냅샷에 정본으로 덮어쓴 뒤 `normalize` 로 도메인별로
       분리·정규화하고,
    3. 메인 에이전트를 실행한 다음,
    4. 확정 결과를 요청의 `dailyRecordId` 에 연결해 저장

하고, 성공/실패를 App Server 콜백으로 통보한다. AI 서버는 task 상태를 직접
보관하지 않는다(상태는 App Server 가 소유). 콜백은 SUCCESS/FAILED 통보만 하고,
실제 결과는 App Server 가 staging DB 에서 읽는다.

전체 메인 에이전트는 `pipeline_timeout_sec` 로 감싼다. timeout 이나 예기치 못한
오류가 나면 FAILED 로 콜백하고, partial 결과는 저장하지 않는다.

실패는 **원인별 정수 코드**(`app.core.error_codes`)로 식별한다. 콜백·관측 이벤트·
운영 로그가 모두 같은 코드 하나를 쓴다.

    스냅샷 없음        → 1101  SOURCE_SNAPSHOT_NOT_FOUND
    원본 계약 위반     → 1102  SOURCE_CONTRACT_VIOLATION   (SourceBatchError)
    파이프라인 timeout → 1201  PIPELINE_TIMEOUT
    구조화 출력 실패   → 1202  STRUCTURED_OUTPUT_INVALID   (StructuredOutputError)
    저장 검증 실패     → 1301  TIMELINE_STORAGE_VALIDATION_FAILED
    DB 오류            → 1302  DATABASE_ERROR
    그 밖              → 1901  INTERNAL_ERROR

콜백 `error` 에는 코드에 묶인 안전 메시지만 싣는다. 원본 예외 메시지는 로그에만
남는다 — 콜백 본문은 AI 서버 밖으로 나가므로 내부 식별자·경로가 실리면 안 된다.

처리 전 구간은 task 단위 관측 컨텍스트(`observation_context`)로 감싼다. 상관키는
`taskId` 하나이며, `contextvars` 로 열려 있어 `asyncio.to_thread` 로 도는 Event/
Timeline/Repair Agent 와 그 안의 LLM 호출까지 같은 taskId 로 이어진다. 관측 이벤트는
실행 중 메모리 버퍼에 모았다가, **콜백을 먼저 보낸 뒤** finally 에서 flush 한다.
관측 flush 는 부가 기능이라 어떤 실패도 Timeline 결과로 전파하지 않는다.
"""

import asyncio

from app.agents.main import run_main_agent
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import code_of, report_error
from app.core.inflight import track_inflight
from app.core.logging import get_logger
from app.core.observability import (
    ObservationEventType,
    ObservationStage,
    emit_observation,
    observation_context,
)
from app.core.observability.runtime import build_task_observer, flush_task_observations
from app.schemas import TaskStatus, TimelineCallbackPayload
from app.schemas.source_snapshot import TimelineWindow
from app.services.callback import send_callback
from app.services.normalizer import normalize
from app.services.source_repository import SourceRepository
from app.services.timeline_repository import TimelineRepository
from app.services.timeline_validator import TimelineValidationError

logger = get_logger(__name__)


async def process_timeline_task(
    task_id: str,
    repo: SourceRepository,
    timeline_repo: TimelineRepository,
    daily_record_id: int,
    window_start: str,
    window_end: str,
    callback_token: str,
) -> TaskStatus:
    """스냅샷을 불러와 정규화·실행·저장하고 성공/실패를 콜백한다.

    AI 서버는 상태를 보관하지 않으므로(App Server 소유), 결과는 콜백으로만 통보하고
    최종 상태를 반환값으로 돌려준다(호출부/테스트 관찰용). 저장/검증 실패는 예외로
    잡혀 FAILED 가 되며, partial 저장은 트랜잭션 롤백으로 남지 않는다.

    전체를 `track_inflight` 로 감싼다. HTTP 응답(202)은 이미 나간 뒤이므로,
    AgentCore Runtime 이 `GET /ping` 으로 유휴 여부를 물었을 때 여기가 아직 돌고
    있다는 사실을 알려야 컨테이너가 회수되지 않는다.
    """

    with track_inflight():
        observer, buffer = build_task_observer()
        logger.info(
            "관측 task 초기화: taskId=%s, collectionEnabled=%s, "
            "obsEnabled=%s, esUrlConfigured=%s, localOutputConfigured=%s",
            task_id,
            observer is not None,
            settings.obs_enabled,
            bool(settings.es_url),
            bool(settings.obs_local_dir),
        )
        try:
            if observer is None:
                status = await _process_observed(
                    task_id,
                    repo,
                    timeline_repo,
                    daily_record_id,
                    window_start,
                    window_end,
                    callback_token,
                )
            else:
                with observation_context(task_id, observer):
                    status = await _process_observed(
                        task_id,
                        repo,
                        timeline_repo,
                        daily_record_id,
                        window_start,
                        window_end,
                        callback_token,
                    )
        finally:
            # 콜백까지 끝난 뒤 관측을 내보낸다. 비활성화된 경우에도 그 이유를
            # 운영 로그에 남긴다. flush 는 내부에서 모든 실패를 흡수하므로 Timeline
            # status·RDB 저장·콜백에 절대 영향을 주지 않는다(완전 격리).
            await flush_task_observations(buffer, task_id=task_id)

    return status


async def _process_observed(
    task_id: str,
    repo: SourceRepository,
    timeline_repo: TimelineRepository,
    daily_record_id: int,
    window_start: str,
    window_end: str,
    callback_token: str,
) -> TaskStatus:
    """관측 컨텍스트가 열린 상태에서 실제 처리·저장·콜백을 수행한다.

    단계마다 관측 이벤트를 남긴다: REQUEST(입력 조회·정규화) → (MAIN_AGENT 이하는
    하위에서) → STORAGE(RDB 저장) → CALLBACK(콜백) → FINAL(성공/실패/timeout).
    """

    status = TaskStatus.SUCCESS
    # 실패 원인을 식별하는 카탈로그 코드. 성공이면 None 이다. 콜백·관측·로그가
    # 모두 이 하나를 쓰므로 세 곳이 다른 값을 말할 수 없다.
    failure_code: ErrorCode | None = None
    active_stage = ObservationStage.REQUEST

    try:
        emit_observation(
            ObservationEventType.STARTED,
            stage=ObservationStage.REQUEST,
            payload={
                "taskId": task_id,
                "dailyRecordId": daily_record_id,
                "window": {"start": window_start, "end": window_end},
            },
        )
        snapshot = await repo.get(task_id)
        if snapshot is None:
            status = TaskStatus.FAILED
            failure_code = report_error(
                logger,
                ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND,
                "수집 스냅샷을 찾지 못함",
                context={"taskId": task_id},
                stage=ObservationStage.REQUEST,
                payload={"reason": "snapshot_not_found"},
            )
        else:
            # 요청이 준 window 를 정본으로 덮어쓴다. 원본 스냅샷 객체(인메모리 스텁이
            # 공유할 수 있음)를 건드리지 않도록 copy 후 교체한다.
            snapshot = snapshot.model_copy(
                update={
                    "timeline_window": TimelineWindow(
                        start_time=window_start, end_time=window_end
                    )
                }
            )
            request = normalize(snapshot)
            emit_observation(
                ObservationEventType.COMPLETED,
                stage=ObservationStage.REQUEST,
                payload={
                    "inputItemCounts": request.source_item_counts(),
                    "hasUserMemory": request.user_memory is not None,
                    "request": request.model_dump(by_alias=True, mode="json"),
                },
            )
            active_stage = ObservationStage.MAIN_AGENT
            draft = await asyncio.wait_for(
                run_main_agent(request),
                timeout=settings.pipeline_timeout_sec,
            )
            # 확정 결과를 timeline_events/timeline_items/timeline_event_items 에
            # 저장한다. 저장/검증 실패는 아래 except 로 잡혀 FAILED 로 처리된다.
            active_stage = ObservationStage.STORAGE
            emit_observation(
                ObservationEventType.STARTED,
                stage=ObservationStage.STORAGE,
                payload={"dailyRecordId": daily_record_id},
            )
            await timeline_repo.save(task_id, draft, daily_record_id)
            emit_observation(
                ObservationEventType.COMPLETED,
                stage=ObservationStage.STORAGE,
                payload={
                    "dailyRecordId": daily_record_id,
                    "eventCount": len(draft.events),
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
            stage=ObservationStage.MAIN_AGENT,
            payload={"timeoutSec": settings.pipeline_timeout_sec},
        )
    except Exception as exc:  # noqa: BLE001 - 백그라운드 최종 방어선
        status = TaskStatus.FAILED
        # 도메인 예외는 자기 코드를 갖고 있고(SourceBatchError 1102,
        # TimelineValidationError 1301, StructuredOutputError 1202 …), 분류되지
        # 않은 예외만 INTERNAL_ERROR 로 떨어진다.
        failure_payload: dict[str, object] = {}
        if isinstance(exc, TimelineValidationError):
            failure_payload = {
                "violationCodes": exc.violation_codes,
                "violationCount": len(exc.details),
            }
        failure_code = report_error(
            logger,
            code_of(exc),
            "타임라인 처리 실패",
            exc=exc,
            context={"taskId": task_id},
            stage=active_stage,
            payload=failure_payload,
            exc_info=True,
        )

    # 설정된 App Server API URL 이 있으면 성공/실패 통보를 전달한다.
    # (관측 flush 보다 콜백을 먼저 보낸다 — 콜백이 주 결과 통보다.)
    if settings.app_server_api_url:
        emit_observation(
            ObservationEventType.STARTED,
            stage=ObservationStage.CALLBACK,
            payload={"status": status.value},
        )
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
            else TimelineCallbackPayload.failure(
                failure_code or ErrorCode.INTERNAL_ERROR
            )
        )
        sent = await send_callback(
            settings.app_server_api_url,
            task_id,
            callback_token,
            payload,
        )
        if sent:
            emit_observation(
                ObservationEventType.COMPLETED,
                stage=ObservationStage.CALLBACK,
                payload={"sent": True, "status": status.value},
            )
        else:
            # 콜백 전송 실패는 Timeline 결과를 바꾸지 않는다(이미 저장은 끝났다).
            # 다만 App Server 가 결과를 못 받은 상태이므로 코드로 남겨 추적한다.
            report_error(
                logger,
                ErrorCode.CALLBACK_SEND_FAILED,
                "완료 콜백 전송 실패",
                context={"taskId": task_id, "status": status.value},
                stage=ObservationStage.CALLBACK,
                payload={"sent": False, "status": status.value},
            )

    emit_observation(
        ObservationEventType.COMPLETED
        if status == TaskStatus.SUCCESS
        else ObservationEventType.FAILED,
        stage=ObservationStage.FINAL,
        payload={
            "status": status.value,
            **({"errorCode": int(failure_code)} if failure_code is not None else {}),
        },
    )
    return status
