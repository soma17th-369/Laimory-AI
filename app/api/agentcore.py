"""Amazon Bedrock AgentCore Runtime 컨테이너 계약 어댑터.

AgentCore Runtime 은 컨테이너에 두 개의 경로를 고정으로 요구한다.

- `POST /invocations` — 호출 payload 를 그대로 전달받는 실행 진입점
- `GET /ping` — 헬스체크. `{"status": "Healthy" | "HealthyBusy"}`

## 진입점이 하나라서 envelope 가 필요하다 (#89)

App Server 는 HTTP 로 부를 때 `POST /v1/timeline` 과 `POST /v1/user-memory` 를 경로로
구분한다. AgentCore 는 `InvokeAgentRuntime` 이 payload 를 `/invocations` **한 곳**으로만
넘기므로 경로가 그 일을 못 한다. 그래서 요청 종류를 body 최상위에 명시한다.

.. code-block:: json

    {"requestType": "TIMELINE" | "USER_MEMORY_UPDATE", "payload": { ... }}

`payload` 안은 해당 엔드포인트의 요청 body **그대로**다. 필드를 골라 담거나 이름을
바꾸지 않으므로 입력 파라미터가 누락될 여지가 없다.

`requestType` 이 `payload` 의 스키마를 **결정**한다(discriminated union). payload 안을
뒤져 종류를 추측하지 않는다 — `taskId`·`taskToken` 은 양쪽에 다 있고 나머지는 선택
필드라, 모양으로 고르면 안 보낸 필드 하나 때문에 엉뚱한 파이프라인으로 들어간다.

## envelope 없는 body 도 계속 받는다

App Server 는 `/invocations` 에 Timeline 요청 body 를 envelope 없이 그대로 보내기도
한다. 이건 **영구히 지원하는 두 번째 형태**이며 전환 기간용 임시 호환이 아니다.
:meth:`InvocationRequest._wrap_bare_timeline` 이 그 body 를 TIMELINE envelope 로 감싸
아래 경로를 하나로 합친다. 이때도 **`requestType` 키가 있는지만** 보고 payload 안은
들여다보지 않는다.

## 처리 구현은 여기 없다

`/invocations` 는 어댑터다. Timeline 은 :func:`~app.api.v1.timeline.create_timeline_draft`,
User Memory 는 :func:`~app.api.v1.user_memory.update_user_memory` 를 그대로 부른다.
구현이 두 벌이 되면 한쪽만 고치는 사고가 난다. `POST /v1/timeline` 과
`POST /v1/user-memory` 도 계속 열려 있어 App Server 는 두 경로를 모두 쓸 수 있다.

## `/ping`

백그라운드 처리가 남아 있으면 `HealthyBusy` 를 돌려준다. AI 서버는 요청을 202 로
접수하고 실제 처리를 백그라운드에서 이어가므로, 유휴로 보고하면 응답 직후 컨테이너가
회수되어 처리가 통째로 날아갈 수 있다. Timeline·User Memory 두 runner 가 모두
`track_inflight` 를 쓰므로 어느 쪽으로 접수됐든 같은 계약이 성립한다.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import Field, RootModel, model_validator

from app.api.error_handlers import ERROR_RESPONSES
from app.api.v1.timeline import (
    TimelineDispatchResponse,
    TimelineTriggerRequest,
    create_timeline_draft,
)
from app.api.v1.user_memory import update_user_memory
from app.core.inflight import is_busy
from app.schemas.common import CamelModel
from app.schemas.user_memory_update import (
    UserMemoryUpdateRequest,
    UserMemoryUpdateResponse,
)
from app.services.app_server_client import AppServerClient, get_app_server_client

router = APIRouter()


class RuntimeHealth(StrEnum):
    """AgentCore Runtime 이 인식하는 헬스체크 상태값."""

    HEALTHY = "Healthy"
    HEALTHY_BUSY = "HealthyBusy"


class PingResponse(CamelModel):
    """`GET /ping` 응답."""

    status: RuntimeHealth


class InvocationRequestType(StrEnum):
    """`/invocations` 가 받는 요청 종류.

    값 하나가 접수 엔드포인트 하나에 대응한다. 헬스·진단(`/ping`·`/health`·
    `/debug/env`)은 여기 넣지 않는다 — AgentCore 는 `/ping` 을 직접 호출하고,
    `/debug/env` 를 외부에서 닿게 만들면 노출면만 넓어진다.
    """

    TIMELINE = "TIMELINE"
    USER_MEMORY_UPDATE = "USER_MEMORY_UPDATE"


class TimelineInvocation(CamelModel):
    """타임라인 생성 접수. `payload` 는 `POST /v1/timeline` body 그대로다."""

    request_type: Literal[InvocationRequestType.TIMELINE] = Field(alias="requestType")
    payload: TimelineTriggerRequest


class UserMemoryInvocation(CamelModel):
    """User Memory 갱신 접수. `payload` 는 `POST /v1/user-memory` body 그대로다."""

    request_type: Literal[InvocationRequestType.USER_MEMORY_UPDATE] = Field(
        alias="requestType"
    )
    payload: UserMemoryUpdateRequest


#: `requestType` 값이 `payload` 스키마를 결정한다. 새 접수 엔드포인트를 추가하면
#: 여기에 항목을 더한다(빠뜨리면 `tests/api/test_agentcore_endpoint.py` 의 커버리지
#: 가드가 실패한다).
InvocationVariant = Annotated[
    TimelineInvocation | UserMemoryInvocation,
    Field(discriminator="request_type"),
]


class InvocationRequest(RootModel[InvocationVariant]):
    """`/invocations` 요청 body.

    envelope 가 정본이고, envelope 없는 Timeline body 도 계속 받는다(모듈 docstring).
    """

    root: InvocationVariant

    @model_validator(mode="before")
    @classmethod
    def _wrap_bare_timeline(cls, data: Any) -> Any:
        """envelope 가 없는 body 를 TIMELINE envelope 로 감싼다.

        판단 기준은 **`requestType` 키의 존재 여부 하나**다. payload 안의 필드를 보고
        종류를 고르지 않는다. 그래야 "무엇을 보냈는지" 가 아니라 "무엇이라고 말했는지"
        로만 갈린다.
        """

        if isinstance(data, dict) and "requestType" not in data and "request_type" not in data:
            return {
                "requestType": InvocationRequestType.TIMELINE,
                "payload": data,
            }
        return data


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
    response_model=TimelineDispatchResponse | UserMemoryUpdateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    tags=["agentcore"],
)
async def invocations(
    request: InvocationRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    client: AppServerClient = Depends(get_app_server_client),
) -> TimelineDispatchResponse | UserMemoryUpdateResponse:
    """AgentCore Runtime 호출 진입점. `requestType` 에 따라 기존 핸들러로 위임한다.

    두 응답 모두 `{taskId, status}` 로 모양이 같다. 202 는 완료가 아니라 접수이며,
    최종 상태는 Timeline 이면 완료 콜백, User Memory 면 결과 저장 호출로 통보한다.
    """

    invocation = request.root
    if isinstance(invocation, UserMemoryInvocation):
        return await update_user_memory(
            invocation.payload, background_tasks, http_request, client
        )
    return await create_timeline_draft(
        invocation.payload, background_tasks, http_request, client
    )
