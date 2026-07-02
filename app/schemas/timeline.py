"""Timeline draft output schema.

Timeline Agent returns this draft after merging and validating
``AiEventCandidate`` values from data-specific Event Agents.
"""

from enum import Enum

from pydantic import AwareDatetime, Field, model_validator

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
    start_time: AwareDatetime = Field(alias="startTime")
    end_time: AwareDatetime = Field(alias="endTime")
    confidence: float = Field(ge=0.0, le=1.0)
    inference_level: InferenceLevel = Field(alias="inferenceLevel")
    source_refs: list[SourceRef] = Field(alias="sourceRefs", min_length=1)
    uncertainty: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_range(self) -> "TimelineEventDraft":
        if self.end_time < self.start_time:
            raise ValueError("endTime must be greater than or equal to startTime")
        return self


# Backward-compatible export name used by existing imports.
TimelineDraftEvent = TimelineEventDraft


class TimelineQuestion(CamelModel):
    """Question that asks the user to resolve an uncertain time range."""

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
