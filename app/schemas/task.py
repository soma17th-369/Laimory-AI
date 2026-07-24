"""Timeline draft task 상태/콜백 계약.

AI 서버는 task 상태를 직접 보관하지 않는다(상태는 App Server 가 소유). 여기서는
처리 상태 값(`TaskStatus`)과, 완료 시 App Server 로 성공/실패를 통보할 콜백
payload(`TimelineCallbackPayload`)만 정의한다.

`taskId` 는 이 처리 단위를 식별한다.
"""

from enum import Enum

from pydantic import Field, field_validator

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
    """완료 시 App Server 콜백 API body로 전달하는 상태 통보.

    App Server 는 이 콜백으로 처리 완료(SUCCESS/FAILED)만 통보받고, 실제 결과는
    staging DB(`timeline_events`/`timeline_items`/`timeline_event_items`)에서 읽는다.
    `taskId` 는 URL path, `callbackToken` 은 `Callback-Token` 헤더로 전달하므로
    body에는 API 서버 `DraftTaskCallbackRequest`와 같은 세 필드만 둔다.
    """

    status: TaskStatus
    error_code: str | None = Field(default=None, alias="errorCode")
    error: str | None = None

    @field_validator("status")
    @classmethod
    def validate_terminal_status(cls, value: TaskStatus) -> TaskStatus:
        """완료 콜백에는 terminal 상태만 허용한다."""

        if value not in {TaskStatus.SUCCESS, TaskStatus.FAILED}:
            raise ValueError("완료 콜백 status는 SUCCESS 또는 FAILED여야 합니다.")
        return value
