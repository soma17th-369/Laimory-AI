"""타임라인 초안 처리 실행부.

`POST /v1/timeline` 이 `taskId` 만 받아 즉시 응답을 돌려준 뒤, 백그라운드에서

    1. 저장소(`SourceRepository`)에서 `taskId` 로 수집 스냅샷을 읽어오고,
    2. `normalize` 로 도메인별로 분리·정규화한 다음,
    3. 메인 에이전트를 실행

하고 상태를 갱신한다(성공/실패). 완료 후 `settings.callback_url` 이 설정돼
있으면 App Server 로 결과를 콜백한다.

전체 메인 에이전트는 `pipeline_timeout_sec` 로 감싼다. timeout 이나 예기치 못한
오류가 나면 task 를 FAILED 로 표시하고, partial 결과는 저장하지 않는다.
"""

import asyncio

from app.agents.main import run_main_agent
from app.core.config import settings
from app.core.logging import get_logger
from app.core.observability import (
    ObservationEventType,
    ObservationStage,
    Observer,
    emit_observation,
    observation_context,
)
from app.schemas import TimelineCallbackPayload
from app.services.callback import send_callback
from app.services.normalizer import normalize
from app.services.source_repository import SourceRepository
from app.services.task_store import TaskStore

logger = get_logger(__name__)


async def process_timeline_task(
    task_id: str,
    store: TaskStore,
    repo: SourceRepository,
    transaction_id: str | None = None,
    observer: Observer | None = None,
) -> None:
    """백그라운드에서 스냅샷을 불러와 정규화·실행하고 상태/콜백을 처리한다."""

    stored = store.get(task_id)
    active_transaction_id = (
        transaction_id
        or (stored.transaction_id if stored is not None else None)
        or task_id
    )
    active_observer = observer or Observer()

    with observation_context(active_transaction_id, active_observer):
        emit_observation(
            ObservationEventType.STARTED,
            stage=ObservationStage.REQUEST,
            payload={"taskId": task_id},
        )
        try:
            snapshot = repo.get(task_id)
            if snapshot is None:
                logger.warning("수집 스냅샷을 찾지 못함: taskId=%s", task_id)
                emit_observation(
                    ObservationEventType.FAILED,
                    stage=ObservationStage.REQUEST,
                    payload={"taskId": task_id, "error": "snapshot_not_found"},
                )
                record = store.mark_failed(task_id, "수집 데이터를 찾지 못했습니다.")
            else:
                request = normalize(
                    snapshot,
                    transaction_id=active_transaction_id,
                )
                emit_observation(
                    ObservationEventType.COMPLETED,
                    stage=ObservationStage.REQUEST,
                    payload={
                        "snapshot": snapshot.model_dump(by_alias=True, mode="json"),
                        "normalizedRequest": request.model_dump(
                            by_alias=True,
                            mode="json",
                        ),
                    },
                )
                draft = await asyncio.wait_for(
                    run_main_agent(request),
                    timeout=settings.pipeline_timeout_sec,
                )
                record = store.mark_success(task_id, draft)
                emit_observation(
                    ObservationEventType.COMPLETED,
                    stage=ObservationStage.FINAL,
                    payload={
                        "timeline": draft.model_dump(by_alias=True, mode="json")
                    },
                )
        except asyncio.TimeoutError:
            error = f"메인 에이전트 timeout ({settings.pipeline_timeout_sec}s) 초과"
            logger.warning("타임라인 처리 timeout: taskId=%s", task_id)
            emit_observation(
                ObservationEventType.FAILED,
                stage=ObservationStage.FINAL,
                payload={"error": error},
            )
            record = store.mark_failed(task_id, error)
        except Exception as exc:  # noqa: BLE001 - 백그라운드 최종 방어선
            logger.warning(
                "타임라인 처리 실패: taskId=%s, error=%s", task_id, exc, exc_info=True
            )
            emit_observation(
                ObservationEventType.FAILED,
                stage=ObservationStage.FINAL,
                payload={"error": str(exc), "errorType": type(exc).__name__},
            )
            record = store.mark_failed(task_id, str(exc))

    # 설정된 콜백 URL 이 있으면 성공/실패 결과를 App Server 로 전달한다.
    if settings.callback_url:
        payload = TimelineCallbackPayload.from_record(record)
        await send_callback(settings.callback_url, payload)
