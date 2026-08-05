"""타임라인 초안 생성 엔드포인트.

`POST /v1/timeline` 은 `taskId`, 작업 전체에 쓰는 `taskToken`, 이벤트를 연결할
`dailyRecordId`, 대상 시간 창 `window` 를 받아 처리 task 를 만들고(PROCESSING) 즉시
응답을 돌려준다(HTTP 종료). 실제 처리는 백그라운드에서 진행되는데, App Server
입력 조회 API 로 수집 원본을 읽어와 정규화한 뒤 메인 에이전트를 실행하고, 결과를
App Server 결과 저장 API 로 보낸 다음 완료를 콜백한다.

`taskToken` 은 이 요청 body 로 받는 **최초 값**이다. 그 뒤로는 App Server 응답
body 가 주는 값으로 갱신되며, AI 서버가 보내는 모든 요청은 최신 토큰을
`Task-Token` 헤더로 싣는다(`app.services.app_server_client`).

AI 서버는 task 상태를 보관하지 않으며(상태는 App Server 소유), 별도 상태 조회
엔드포인트는 두지 않는다.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import AwareDatetime, Field, model_validator

from app.api.error_handlers import ERROR_RESPONSES
from app.api.request_logging import annotate_request_task
from app.schemas import TaskStatus
from app.schemas.common import CamelModel
from app.services.app_server_client import AppServerClient, get_app_server_client
from app.services.timeline_runner import process_timeline_task

router = APIRouter()


class TimelineWindowPayload(CamelModel):
    """초안 생성 대상 시간 창. 수집 원본에서 파생하지 않고 요청이 정본으로 준다.

    경계에 기상·취침 같은 생활 의미는 없다(#67). 결과 event 가 벗어날 수 없는 범위이며,
    이 값이 뒤 단계 전체의 검증 기준이 된다.
    """

    start_at: AwareDatetime = Field(alias="startAt")
    end_at: AwareDatetime = Field(alias="endAt")

    @model_validator(mode="after")
    def _validate_order(self) -> "TimelineWindowPayload":
        # 역전된 window 는 예전에 범위 검증을 통째로 끄는 입구였다(#67). 이제는 접수
        # 단계에서 거절한다 — 검증할 수 없는 요청을 202 로 받아 두는 것이 더 나쁘다.
        if self.end_at <= self.start_at:
            raise ValueError("window.endAt 은 startAt 보다 뒤여야 합니다")
        return self


class TimelineTriggerRequest(CamelModel):
    """초안 생성 요청.

    수집 데이터는 App Server 가 갖고 있고, 여기서는 `taskId`, 이후 모든 App Server
    호출에 쓸 `taskToken`, 이벤트를 연결할 `dailyRecordId`, 대상 시간 창 `window`
    를 받는다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    task_token: str = Field(alias="taskToken", min_length=1)
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
    payload: TimelineTriggerRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    client: AppServerClient = Depends(get_app_server_client),
) -> TimelineDispatchResponse:
    """`taskId` 로 타임라인 초안 생성을 접수한다.

    실제 처리(입력 조회 → 정규화 → 메인 에이전트 → 결과 저장 → 콜백)를 백그라운드로
    넘기고, 즉시 taskId 와 PROCESSING 을 반환한다. AI 서버는 task 상태를 보관하지
    않으며(상태는 App Server 소유), 결과는 콜백으로만 통보한다.

    접수 이벤트(202)에 `taskId` 를 실어, 나중에 나올 백그라운드 완료 이벤트와 같은
    상관키로 잇는다. `taskToken` 과 `window` 는 넘기지 않는다.
    """

    annotate_request_task(http_request, payload.task_id)
    background_tasks.add_task(
        process_timeline_task,
        payload.task_id,
        client,
        payload.daily_record_id,
        payload.window.start_at.isoformat(),
        payload.window.end_at.isoformat(),
        payload.task_token,
    )

    return TimelineDispatchResponse(
        task_id=payload.task_id,
        status=TaskStatus.PROCESSING,
    )
