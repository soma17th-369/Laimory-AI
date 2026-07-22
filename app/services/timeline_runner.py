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
"""

import asyncio

from app.agents.main import run_main_agent
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas import TaskStatus, TimelineCallbackPayload
from app.schemas.source_snapshot import TimelineWindow
from app.services.callback import send_callback
from app.services.normalizer import normalize
from app.services.source_repository import SourceRepository
from app.services.timeline_repository import TimelineRepository

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
    """

    status = TaskStatus.SUCCESS
    try:
        snapshot = await repo.get(task_id)
        if snapshot is None:
            logger.warning("수집 스냅샷을 찾지 못함: taskId=%s", task_id)
            status = TaskStatus.FAILED
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
            draft = await asyncio.wait_for(
                run_main_agent(request),
                timeout=settings.pipeline_timeout_sec,
            )
            # 확정 결과를 timeline_events/timeline_items/timeline_event_items 에
            # 저장한다. 저장/검증 실패는 아래 except 로 잡혀 FAILED 로 처리된다.
            await timeline_repo.save(task_id, draft, daily_record_id)
    except asyncio.TimeoutError:
        logger.warning("타임라인 처리 timeout: taskId=%s", task_id)
        status = TaskStatus.FAILED
    except Exception as exc:  # noqa: BLE001 - 백그라운드 최종 방어선
        logger.warning(
            "타임라인 처리 실패: taskId=%s, error=%s", task_id, exc, exc_info=True
        )
        status = TaskStatus.FAILED

    # 설정된 콜백 URL 이 있으면 성공/실패 통보를 App Server 로 전달한다.
    if settings.callback_url:
        payload = TimelineCallbackPayload(
            task_id=task_id,
            callback_token=callback_token,
            status=status,
        )
        await send_callback(settings.callback_url, payload)
    return status
