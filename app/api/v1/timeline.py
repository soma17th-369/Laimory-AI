"""타임라인 초안 생성 엔드포인트.

`POST /v1/timeline` 은 `taskId` 만 받아 처리 task 를 만들고(PROCESSING) 즉시
응답을 돌려준다(HTTP 종료). 실제 처리는 백그라운드에서 진행되는데, `taskId`
로 DB(`SourceRepository`)에서 수집 스냅샷을 읽어와 정규화한 뒤 메인 에이전트를
실행한다. 완료 시 상태를 갱신하고, `settings.callback_url` 이 설정돼 있으면
결과를 App Server 로 콜백한다. `GET /v1/timeline/{taskId}` 로 처리 상태와
결과를 조회할 수 있다.
"""

from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import Field

from app.schemas import TaskRecord, TaskStatus
from app.schemas.common import CamelModel
from app.services.source_repository import SourceRepository, get_source_repository
from app.services.task_store import TaskStore, get_task_store
from app.services.timeline_runner import process_timeline_task

router = APIRouter()


class TimelineTriggerRequest(CamelModel):
    """초안 생성 요청. 수집 데이터는 DB 에 있고, 여기서는 `taskId` 만 받는다."""

    task_id: str = Field(alias="taskId", min_length=1)


class TimelineDispatchResponse(CamelModel):
    """초안 처리 접수 응답. 실제 결과는 콜백 또는 상태 조회로 받는다."""

    task_id: str = Field(alias="taskId")
    transaction_id: str = Field(alias="transactionId")
    status: TaskStatus


@router.post(
    "",
    response_model=TimelineDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_timeline_draft(
    request: TimelineTriggerRequest,
    background_tasks: BackgroundTasks,
    store: TaskStore = Depends(get_task_store),
    repo: SourceRepository = Depends(get_source_repository),
) -> TimelineDispatchResponse:
    """`taskId` 로 타임라인 초안 생성을 접수한다.

    task 를 PROCESSING 으로 만든 뒤 실제 처리(DB 조회 → 정규화 → 메인 에이전트)를
    백그라운드로 넘기고, 즉시 taskId 를 반환한다.
    """

    transaction_id = f"tx-{uuid4()}"
    record = store.create(request.task_id, transaction_id)

    background_tasks.add_task(
        process_timeline_task,
        request.task_id,
        store,
        repo,
        transaction_id,
    )

    return TimelineDispatchResponse(
        task_id=record.task_id,
        transaction_id=transaction_id,
        status=record.status,
    )


@router.get("/{task_id}", response_model=TaskRecord)
async def get_timeline_task(
    task_id: str,
    store: TaskStore = Depends(get_task_store),
) -> TaskRecord:
    """처리 task 의 상태와 결과를 조회한다."""

    record = store.get(task_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"존재하지 않는 task 입니다: {task_id}",
        )
    return record
