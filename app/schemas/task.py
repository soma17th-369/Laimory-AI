"""Timeline draft task 상태/콜백 계약.

AI 서버는 task 상태를 직접 보관하지 않는다(상태는 App Server 가 소유). 여기서는
처리 상태 값(`TaskStatus`)과, 완료 시 App Server 로 성공/실패를 통보할 콜백
payload(`TimelineCallbackPayload`)만 정의한다.

`taskId` 는 이 처리 단위를 식별한다.
"""

from enum import Enum

from pydantic import Field, field_validator, model_validator

from app.core.error_codes import RESERVED_CODES, ErrorCode, message_for
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

    `errorCode` 는 실패 원인을 식별하는 **정수**다(:mod:`app.core.error_codes`).
    구 계약은 `"ERROR_1008"` 문자열 하나로 모든 실패를 표현했지만, 그러면 App Server
    가 원인을 구분할 수 없고 문자열 파싱에 의존하게 된다. 지금은 원인별로 다른
    정수가 나가며, App Server 는 모르는 코드를 총괄 실패로 다루면 된다.

    `error` 는 카탈로그의 **안전 메시지**만 담는다. 원본 예외 메시지는 로그에만
    남는다 — 콜백 본문은 AI 서버 밖으로 나가므로 내부 식별자·경로가 실리면 안 된다.
    """

    status: TaskStatus
    error_code: int | None = Field(default=None, alias="errorCode")
    error: str | None = None

    @field_validator("status")
    @classmethod
    def validate_terminal_status(cls, value: TaskStatus) -> TaskStatus:
        """완료 콜백에는 terminal 상태만 허용한다."""

        if value not in {TaskStatus.SUCCESS, TaskStatus.FAILED}:
            raise ValueError("완료 콜백 status는 SUCCESS 또는 FAILED여야 합니다.")
        return value

    @model_validator(mode="after")
    def validate_error_fields(self) -> "TimelineCallbackPayload":
        """상태와 오류 필드의 짝을 맞춘다.

        성공 콜백에 오류가 실리거나 실패 콜백에 오류가 비어 있으면 App Server 가
        실패 원인을 알 수 없다. 콜백을 만드는 쪽의 실수를 여기서 잡는다.
        """

        if self.status is TaskStatus.SUCCESS:
            if self.error_code is not None or self.error is not None:
                raise ValueError("성공 콜백에는 errorCode/error 를 넣지 않습니다.")
        else:
            if self.error_code is None or not (self.error or "").strip():
                raise ValueError(
                    "실패 콜백에는 errorCode(정수)와 비어 있지 않은 error 가 필요합니다."
                )
            try:
                code = ErrorCode(self.error_code)
            except ValueError as exc:
                raise ValueError(
                    "실패 콜백 errorCode는 오류 코드 카탈로그에 있어야 합니다."
                ) from exc
            if code in RESERVED_CODES:
                raise ValueError("예약된 errorCode는 실패 콜백에 사용할 수 없습니다.")
            if self.error != message_for(code):
                raise ValueError(
                    "실패 콜백 error는 오류 코드 카탈로그의 안전 메시지여야 합니다."
                )
        return self

    @classmethod
    def success(cls) -> "TimelineCallbackPayload":
        """성공 콜백. 오류 필드는 `null` 로 나간다(기존 계약 유지)."""

        return cls(status=TaskStatus.SUCCESS)

    @classmethod
    def failure(cls, code: ErrorCode) -> "TimelineCallbackPayload":
        """실패 콜백. 코드에 묶인 안전 메시지만 싣는다."""

        return cls(
            status=TaskStatus.FAILED,
            error_code=int(code),
            error=message_for(code),
        )
