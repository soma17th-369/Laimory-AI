"""Timeline draft output schema.

Timeline Agent returns this draft after merging and validating
``AiEventCandidate`` values from data-specific Event Agents.
"""

from enum import Enum

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from app.schemas.common import CamelModel
from app.schemas.event_candidate import EventType, InferenceLevel, SourceRef


class TimelineWarningSeverity(str, Enum):
    """Warning severity for a recoverable draft quality issue."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TimelineEventDraft(CamelModel):
    """Single editable event in the final daily timeline draft."""

    client_event_id: str = Field(
        alias="clientEventId",
        min_length=1,
        description="Client-side draft event id assigned by Timeline Agent.",
    )
    event_type: EventType = Field(alias="eventType")
    title: str = Field(min_length=1)
    description: str = Field(default="")
    address: str | None = None
    #: 이 event 가 있었던 장소명. 파이프라인 전체가 같은 이름을 쓴다 — 입력의
    #: `stay.place`, candidate 의 `places`(고를 후보), 결과 저장 계약의 `place`.
    #: 예전 이름 `placeLabel` 도 계속 받는다. 동결된 프롬프트(`timeline_v2.0.0.md`)나
    #: 이전 버전으로 롤백해 모델이 옛 키를 내도 값이 조용히 사라지지 않게 하기 위한
    #: 것이다 — 이 모델은 모르는 키를 무시하므로 alias 가 없으면 장소가 그냥 없어진다.
    place: str | None = Field(
        default=None,
        validation_alias=AliasChoices("place", "placeLabel"),
    )
    tags: list[str] = Field(default_factory=list)
    start_time: AwareDatetime = Field(alias="startTime")
    end_time: AwareDatetime = Field(alias="endTime")
    confidence: float = Field(ge=0.0, le=1.0)
    inference_level: InferenceLevel = Field(alias="inferenceLevel")
    source_refs: list[SourceRef] = Field(alias="sourceRefs", min_length=1)
    uncertainty: list[str] = Field(default_factory=list)
    question: str | None = Field(
        default=None,
        description=(
            "이 event 를 두고 사용자에게 던지는 회고 유도 질문(이슈 #66). Repair 가 "
            "끝난 뒤 Question Agent 가 채우며, 저장 계약의 `question` 으로 나간다. "
            "상위 TimelineDraft.questions(모호성 확인 질문)와 목적도 수신자도 다르다."
        ),
    )

    @model_validator(mode="after")
    def _validate_range(self) -> "TimelineEventDraft":
        if self.end_time < self.start_time:
            raise ValueError("endTime must be greater than or equal to startTime")
        return self


# Backward-compatible export name used by existing imports.
TimelineDraftEvent = TimelineEventDraft


class TimelineQuestion(CamelModel):
    """Question that asks the user to resolve an uncertain time range.

    **내부 전용이다.** 시간·장소가 모호할 때 그 사실을 남기는 확인 질문이며 App
    Server 로 나가지 않는다. 사용자가 읽는 회고 유도 질문은 이것이 아니라
    :attr:`TimelineEventDraft.question` 이다(이슈 #66).
    """

    question_id: str = Field(alias="questionId", min_length=1)
    time_range: dict[str, AwareDatetime] = Field(alias="timeRange")
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    related_event_ids: list[str] = Field(
        default_factory=list,
        alias="relatedEventIds",
        description="TimelineEventDraft.clientEventId values related to this question.",
    )

    @model_validator(mode="after")
    def _validate_time_range(self) -> "TimelineQuestion":
        start = self.time_range.get("startTime")
        end = self.time_range.get("endTime")
        if start is None or end is None:
            raise ValueError("timeRange must include startTime and endTime")
        if end < start:
            raise ValueError("timeRange.endTime must be greater than or equal to startTime")
        return self


class TimelineWarning(CamelModel):
    """Recoverable quality warning for a generated draft."""

    warning_id: str = Field(alias="warningId", min_length=1)
    severity: TimelineWarningSeverity = TimelineWarningSeverity.MEDIUM
    message: str = Field(min_length=1)
    source_refs: list[SourceRef] = Field(default_factory=list, alias="sourceRefs")


class TimelineDraft(CamelModel):
    """Top-level daily timeline draft shown to the user for review."""

    user_id: str = Field(alias="userId", min_length=1)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone: str = Field(min_length=1)
    events: list[TimelineEventDraft] = Field(default_factory=list)
    questions: list[TimelineQuestion] = Field(default_factory=list)
    warnings: list[TimelineWarning] = Field(default_factory=list)
