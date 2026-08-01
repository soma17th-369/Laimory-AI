"""Amazon Bedrock AgentCore Runtime 컨테이너 계약 어댑터.

AgentCore Runtime 은 컨테이너에 두 개의 경로를 고정으로 요구한다.

- `POST /invocations` — 호출 payload 를 그대로 전달받는 실행 진입점
- `GET /ping` — 헬스체크. `{"status": "Healthy" | "HealthyBusy"}`

`/invocations` 는 별도 처리 로직을 갖지 않고 `POST /v1/timeline` 과 완전히 같은
요청 계약(`TimelineTriggerRequest`)을 받아 같은 핸들러로 위임한다. 구현이 두 벌이
되면 한쪽만 고치는 사고가 나므로 어댑터로만 둔다.

`/ping` 은 백그라운드 처리가 남아 있으면 `HealthyBusy` 를 돌려준다. AI 서버는
요청을 202 로 접수하고 실제 처리를 백그라운드에서 이어가므로, 유휴로 보고하면
응답 직후 컨테이너가 회수되어 처리가 통째로 날아갈 수 있다.
"""

from enum import StrEnum

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from app.api.error_handlers import ERROR_RESPONSES
from app.api.v1.timeline import (
    TimelineDispatchResponse,
    TimelineTriggerRequest,
    create_timeline_draft,
)
from app.core.inflight import is_busy
from app.schemas.common import CamelModel
from app.services.app_server_client import AppServerClient, get_app_server_client

router = APIRouter()


class RuntimeHealth(StrEnum):
    """AgentCore Runtime 이 인식하는 헬스체크 상태값."""

    HEALTHY = "Healthy"
    HEALTHY_BUSY = "HealthyBusy"


class PingResponse(CamelModel):
    """`GET /ping` 응답."""

    status: RuntimeHealth


@router.get("/ping", response_model=PingResponse, tags=["agentcore"])
def ping() -> PingResponse:
    """컨테이너 헬스체크.

    진행 중인 백그라운드 처리가 있으면 `HealthyBusy`, 유휴면 `Healthy` 다.
    """

    return PingResponse(
        status=RuntimeHealth.HEALTHY_BUSY if is_busy() else RuntimeHealth.HEALTHY
    )


@router.post(
    "/invocations",
    response_model=TimelineDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    tags=["agentcore"],
)
async def invocations(
    payload: TimelineTriggerRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    client: AppServerClient = Depends(get_app_server_client),
) -> TimelineDispatchResponse:
    """AgentCore Runtime 호출 진입점. `POST /v1/timeline` 과 동일하게 처리한다."""

    return await create_timeline_draft(payload, background_tasks, http_request, client)
