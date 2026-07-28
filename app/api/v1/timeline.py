"""타임라인 초안 생성 엔드포인트.

`POST /v1/timeline` 은 `taskId`, `callbackToken`, 이벤트를 연결할 `dailyRecordId`,
대상 시간 창 `window` 를 받아 처리 task 를 만들고(PROCESSING) 즉시 응답을 돌려준다
(HTTP 종료). 실제 처리는 백그라운드에서 진행되는데, `taskId` 로
DB(`SourceRepository`)에서 수집 스냅샷을 읽어와 정규화한 뒤 메인 에이전트를
실행한다. 완료 시 `settings.app_server_api_url` 이 설정돼 있으면 성공/실패를 App Server
로 콜백한다. AI 서버는 task 상태를 보관하지 않으며(상태는 App Server 소유), 별도
상태 조회 엔드포인트는 두지 않는다.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, status
from pydantic import AwareDatetime, Field

from app.api.error_handlers import ERROR_RESPONSES
from app.schemas import TaskStatus
from app.schemas.common import CamelModel
from app.services.source_repository import SourceRepository, get_source_repository
from app.services.timeline_repository import (
    TimelineRepository,
    get_timeline_repository,
)
from app.services.timeline_runner import process_timeline_task

router = APIRouter()


class TimelineWindowPayload(CamelModel):
    """초안 생성 대상 시간 창. 수집 원본에서 파생하지 않고 요청이 정본으로 준다."""

    start_at: AwareDatetime = Field(alias="startAt")
    end_at: AwareDatetime = Field(alias="endAt")


class TimelineTriggerRequest(CamelModel):
    """초안 생성 요청. 수집 데이터는 DB 에 있고, 여기서는 `taskId`, 콜백 대조용
    `callbackToken`, 이벤트를 연결할 `dailyRecordId`, 대상 시간 창 `window` 를 받는다."""

    task_id: str = Field(alias="taskId", min_length=1)
    callback_token: str = Field(alias="callbackToken", min_length=1)
    daily_record_id: int = Field(alias="dailyRecordId")
    window: TimelineWindowPayload


class TimelineDispatchResponse(CamelModel):
    """초안 처리 접수 응답. 최종 상태는 완료 콜백으로 통보한다."""

    task_id: str = Field(alias="taskId")
    status: TaskStatus


@router.post(
    "",
    response_model=TimelineDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def create_timeline_draft(
    request: TimelineTriggerRequest,
    background_tasks: BackgroundTasks,
    repo: SourceRepository = Depends(get_source_repository),
    timeline_repo: TimelineRepository = Depends(get_timeline_repository),
) -> TimelineDispatchResponse:
    """`taskId` 로 타임라인 초안 생성을 접수한다.

    실제 처리(DB 조회 → 정규화 → 메인 에이전트 → staging DB 저장 → 콜백)를
    백그라운드로 넘기고, 즉시 taskId 와 PROCESSING 을 반환한다. AI 서버는 task
    상태를 보관하지 않으며(상태는 App Server 소유), 결과는 콜백으로만 통보한다.
    """

    background_tasks.add_task(
        process_timeline_task,
        request.task_id,
        repo,
        timeline_repo,
        request.daily_record_id,
        request.window.start_at.isoformat(),
        request.window.end_at.isoformat(),
        request.callback_token,
    )

    return TimelineDispatchResponse(
        task_id=request.task_id,
        status=TaskStatus.PROCESSING,
    )
