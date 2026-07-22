"""Timeline draft task 상태/콜백 계약.

AI 서버는 task 상태를 직접 보관하지 않는다(상태는 App Server 가 소유). 여기서는
처리 상태 값(`TaskStatus`)과, 완료 시 App Server 로 성공/실패를 통보할 콜백
payload(`TimelineCallbackPayload`)만 정의한다.

`taskId` 는 이 처리 단위를 식별한다.
"""

from enum import Enum

from pydantic import Field

from app.schemas.common import CamelModel


class TaskStatus(str, Enum):
    """타임라인 초안 처리 상태.

    - `PROCESSING`: 접수 직후(202 응답 시).
    - `SUCCESS`: 초안 생성·저장 완료.
    - `FAILED`: 실패/timeout. partial 결과는 저장하지 않는다.
    """

    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class TimelineCallbackPayload(CamelModel):
    """완료 시 App Server 콜백 URL 로 전달하는 통보 payload.

    App Server 는 이 콜백으로 처리 완료(SUCCESS/FAILED)만 통보받고, 실제 결과는
    staging DB(`timeline_events`/`timeline_items`/`timeline_event_items`)에서 읽는다.
    그래서 payload 는 전체 결과 body 를 싣지 않고 식별자와 상태만 담는다.
    `callbackToken` 은 요청 시 App Server 가 준 값을 그대로 되돌려 콜백을 대조하게 한다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    callback_token: str = Field(alias="callbackToken", min_length=1)
    status: TaskStatus
