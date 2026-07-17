"""도구 독립적인 Timeline 관측 이벤트 계약."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ObservationStage(StrEnum):
    REQUEST = "REQUEST"
    MAIN_AGENT = "MAIN_AGENT"
    EVENT_AGENT = "EVENT_AGENT"
    TIMELINE_AGENT = "TIMELINE_AGENT"
    REPAIR_AGENT = "REPAIR_AGENT"
    LLM = "LLM"
    FINAL = "FINAL"


class ObservationEventType(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PROMPT = "PROMPT"
    RESPONSE = "RESPONSE"
    PLAN = "PLAN"
    TOOL_CALL = "TOOL_CALL"
    DRAFT_UPDATED = "DRAFT_UPDATED"


class ContentCapture(StrEnum):
    """payload 본문을 관측 sink로 전달하는 수준."""

    NONE = "NONE"
    SANITIZED = "SANITIZED"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ObservationEvent(BaseModel):
    """한 Timeline 처리 흐름에서 발생한 관측 이벤트 하나."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: str = Field(default="1", alias="schemaVersion")
    transaction_id: str = Field(alias="transactionId", min_length=1)
    timestamp: datetime = Field(default_factory=_now_utc)
    stage: ObservationStage
    event_type: ObservationEventType = Field(alias="eventType")
    agent: str | None = None
    iteration: int | None = Field(default=None, ge=1)
    provider: str | None = None
    model: str | None = None
    duration_ms: float | None = Field(default=None, alias="durationMs", ge=0)
    input_tokens: int | None = Field(default=None, alias="inputTokens", ge=0)
    output_tokens: int | None = Field(default=None, alias="outputTokens", ge=0)
    total_tokens: int | None = Field(default=None, alias="totalTokens", ge=0)
    cached_tokens: int | None = Field(default=None, alias="cachedTokens", ge=0)
    reasoning_tokens: int | None = Field(default=None, alias="reasoningTokens", ge=0)
    tool_tokens: int | None = Field(default=None, alias="toolTokens", ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """JSON sink가 사용하는 alias 기반 직렬화 결과를 반환한다."""

        return self.model_dump(by_alias=True, mode="json", exclude_none=True)
