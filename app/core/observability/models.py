"""도구 독립적인 Timeline 관측 이벤트 계약.

상관키는 ``taskId`` 하나다. 요청 접수·DB 원본 조회·결과 저장·callback 을 이미
``taskId`` 가 연결하므로 별도 실행 식별자(transactionId/executionId 등)를 만들지
않는다. 한 ``taskId`` 안에서 병렬(Event Agent)로 쏟아지는 이벤트의 시간순
재구성은 Observer 가 붙이는 task 단위 단조 증가 ``sequence`` 로 한다. 별도
``eventId`` 필드는 두지 않고, Bulk 전송 시 중복 방지용 문서 ``_id`` 가 필요하면
``taskId`` + ``sequence`` 로 전송 시점에 파생한다(계약에는 노출하지 않는다).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ObservationStage(StrEnum):
    """관측 이벤트가 발생한 처리 단계."""

    REQUEST = "REQUEST"
    MAIN_AGENT = "MAIN_AGENT"
    EVENT_AGENT = "EVENT_AGENT"
    TIMELINE_AGENT = "TIMELINE_AGENT"
    REPAIR_AGENT = "REPAIR_AGENT"
    LLM = "LLM"
    STORAGE = "STORAGE"
    CALLBACK = "CALLBACK"
    FINAL = "FINAL"


class ObservationEventType(StrEnum):
    """단계 안에서 일어난 사건의 종류."""

    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PROMPT = "PROMPT"
    RESPONSE = "RESPONSE"
    PLAN = "PLAN"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    DRAFT_UPDATED = "DRAFT_UPDATED"
    VALIDATION_REPAIRED = "VALIDATION_REPAIRED"


class ContentCapture(StrEnum):
    """payload 본문을 관측 sink로 전달하는 수준.

    ``NONE``은 본문 대신 길이와 해시만 남긴다. ``SANITIZED``는 Secret과 식별
    가능한 패턴을 마스킹하고 크기 제한을 적용한 본문을 남긴다. 마스킹하지 않은
    원문 저장 모드는 제공하지 않는다.
    """

    NONE = "NONE"
    SANITIZED = "SANITIZED"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ObservationEvent(BaseModel):
    """한 Timeline 처리 흐름(taskId)에서 발생한 관측 이벤트 하나.

    ``sequence`` 는 생성 시점에는 비어 있고, Observer 가 emit 하는 순간 task 단위
    단조 증가 값으로 채운다(직접 생성한 뒤 emit 하지 않으면 ``None`` 이다).
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: str = Field(default="1", alias="schemaVersion")
    task_id: str = Field(alias="taskId", min_length=1)
    sequence: int | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=_now_utc)
    stage: ObservationStage
    event_type: ObservationEventType = Field(alias="eventType")
    agent: str | None = None
    iteration: int | None = Field(default=None, ge=1)
    provider: str | None = None
    model: str | None = None
    # 버전 출처: agent=배포 env/build, provider=설치 SDK 패키지 버전, model=요청 model ID.
    agent_version: str | None = Field(default=None, alias="agentVersion")
    provider_version: str | None = Field(default=None, alias="providerVersion")
    duration_ms: float | None = Field(default=None, alias="durationMs", ge=0)
    input_tokens: int | None = Field(default=None, alias="inputTokens", ge=0)
    output_tokens: int | None = Field(default=None, alias="outputTokens", ge=0)
    total_tokens: int | None = Field(default=None, alias="totalTokens", ge=0)
    cached_tokens: int | None = Field(default=None, alias="cachedTokens", ge=0)
    reasoning_tokens: int | None = Field(default=None, alias="reasoningTokens", ge=0)
    tool_tokens: int | None = Field(default=None, alias="toolTokens", ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """JSON/ES sink 가 사용하는 alias 기반 직렬화 결과를 반환한다."""

        return self.model_dump(by_alias=True, mode="json", exclude_none=True)
