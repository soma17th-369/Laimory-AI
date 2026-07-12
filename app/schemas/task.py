"""Timeline draft task 상태/콜백 계약.

AI 서버가 App Server 요청을 받아 타임라인 초안을 만드는 동안의 처리 상태를
추적하고(`TaskStatus`/`TaskRecord`), 완료 시 App Server 로 결과를 되돌려줄
콜백 payload(`TimelineCallbackPayload`)를 정의한다.

`taskId` 는 이 처리 단위를 식별한다. 새 입력 계약에는 별도 `transactionId`
가 없어 선택 값으로 두고, 없으면 보통 `taskId` 를 그대로 채운다.
"""

from enum import Enum

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.timeline import TimelineDraft


class TaskStatus(str, Enum):
    """타임라인 초안 처리 상태.

    - `PROCESSING`: 파이프라인 실행 중.
    - `SUCCESS`: 초안 생성 완료(결과 보유).
    - `FAILED`: 실패/timeout. partial 결과는 저장하지 않는다.
    """

    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class TaskRecord(CamelModel):
    """단일 처리 task 의 상태 스냅샷.

    상태 조회 응답과 콜백 payload 구성의 근거가 된다. `result` 는 `SUCCESS`
    일 때만 채워지며, 실패 시에는 partial data 대신 `error` 만 남긴다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    transaction_id: str | None = Field(default=None, alias="transactionId")
    status: TaskStatus = TaskStatus.PROCESSING
    result: TimelineDraft | None = None
    error: str | None = None


class TimelineCallbackPayload(CamelModel):
    """완료 시 App Server 콜백 URL 로 전달하는 결과 payload."""

    task_id: str = Field(alias="taskId", min_length=1)
    transaction_id: str | None = Field(default=None, alias="transactionId")
    status: TaskStatus
    result: TimelineDraft | None = None
    error: str | None = None

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TimelineCallbackPayload":
        """`TaskRecord` 로부터 콜백 payload 를 만든다."""

        return cls(
            task_id=record.task_id,
            transaction_id=record.transaction_id,
            status=record.status,
            result=record.result,
            error=record.error,
        )
