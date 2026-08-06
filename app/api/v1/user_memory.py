"""User Memory 갱신 엔드포인트 (#64).

`POST /v1/user-memory` 는 `taskId`, `taskToken`, 기존 `userMemory`, 확정된 `dailyTimelines`
를 받아 갱신 작업을 접수하고 즉시 응답을 돌려준다(HTTP 종료). 실제 처리는
백그라운드에서 진행되며, 결과는 **App Server 결과 저장 API 한 번**으로 통보한다 —
성공도 실패도 같은 경로다. 완료 콜백은 없다.

App Server 는 202 만 접수 성공으로 보고 수 초 timeout 을 건다. 그래서 이 함수는 어떤
검증도 더 하지 않는다 — 크기 초과조차 여기서 거절하지 않는다(4xx 는 그쪽에서 "미접수
확정" 으로 읽혀 사용자에게 일기 저장 실패로 보인다). 입력이 크면 프롬프트 조립
단계에서 자른다(:mod:`app.services.user_memory_limits`).

AI 서버는 작업 상태를 보관하지 않으며, 상태 조회 엔드포인트도 두지 않는다.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from app.api.error_handlers import ERROR_RESPONSES
from app.api.request_logging import annotate_request_task
from app.schemas import TaskStatus
from app.schemas.user_memory_update import (
    UserMemoryUpdateRequest,
    UserMemoryUpdateResponse,
)
from app.services.app_server_client import AppServerClient, get_app_server_client
from app.services.user_memory_runner import process_user_memory_task

router = APIRouter()


@router.post(
    "",
    response_model=UserMemoryUpdateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def update_user_memory(
    payload: UserMemoryUpdateRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    client: AppServerClient = Depends(get_app_server_client),
) -> UserMemoryUpdateResponse:
    """`taskId` 로 User Memory 갱신을 접수한다.

    접수 이벤트(202)에 `taskId` 를 실어, 나중에 나올 백그라운드 완료 이벤트
    (`usermemory.task.completed`)와 같은 상관키로 잇는다. `taskToken` 과 `dailyTimelines` 는
    넘기지 않는다.
    """

    annotate_request_task(http_request, payload.task_id)
    background_tasks.add_task(
        process_user_memory_task,
        payload.task_id,
        client,
        payload,
    )

    return UserMemoryUpdateResponse(
        task_id=payload.task_id,
        status=TaskStatus.PROCESSING,
    )
