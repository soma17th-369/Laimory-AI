"""Timeline draft output schema.

Timeline Agent returns this draft after merging and validating
``AiEventCandidate`` values from data-specific Event Agents.

LLM 이 쓰는 계약과 파이프라인이 들고 다니는 draft 는 다르다(#118).

- :class:`TimelineAgentOutput` — Timeline Agent 의 **LLM 출력 계약**. event 목록과
  Timeline 만 아는 warning 뿐이다. 구조화 출력 스키마로 provider 에 그대로 실린다.
- :class:`TimelineDraft` — 파이프라인 내부 draft. 여기에 코드가 부여하는
  ``clientEventId``, Question Agent 가 채우는 ``question``, 상위 Agent·guard 가 남기는
  warning 이 더해진다.

두 계약이 event 필드를 공유하도록 :class:`TimelineEventDraft` 가
:class:`TimelineAgentEvent` 를 상속한다. 필드를 손으로 두 번 적으면 LLM 이 쓰는 모양과
코드가 읽는 모양이 말없이 갈린다.

내부 모호성 질문(``questions``)은 #118 에서 없앴다. 읽어서 쓰는 곳이 없었고, 그 자리는
event 의 ``uncertainty`` 와 Timeline 의 warning 이 대신한다. 사용자가 읽는 회고 질문은
:attr:`TimelineEventDraft.question` 이며 그것과는 처음부터 다른 값이었다(#66).
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


class TimelineWarning(CamelModel):
    """Recoverable quality warning for a generated draft."""

    warning_id: str = Field(alias="warningId", min_length=1)
    severity: TimelineWarningSeverity = TimelineWarningSeverity.MEDIUM
    message: str = Field(min_length=1)
    source_refs: list[SourceRef] = Field(default_factory=list, alias="sourceRefs")


class TimelineAgentEvent(CamelModel):
    """Timeline Agent(LLM)가 쓰는 event 한 건. 코드가 덧붙이는 필드는 여기 없다."""

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

    @model_validator(mode="after")
    def _validate_range(self) -> "TimelineAgentEvent":
        if self.end_time < self.start_time:
            raise ValueError("endTime must be greater than or equal to startTime")
        return self


class TimelineEventDraft(TimelineAgentEvent):
    """Single editable event in the final daily timeline draft.

    LLM 이 쓴 :class:`TimelineAgentEvent` 에 코드와 뒤 단계가 채우는 값이 더해진 것이다.
    """

    client_event_id: str = Field(
        alias="clientEventId",
        min_length=1,
        description="Client-side draft event id assigned by Timeline Agent.",
    )
    question: str | None = Field(
        default=None,
        description=(
            "이 event 를 두고 사용자에게 던지는 회고 유도 질문(이슈 #66). Repair 가 "
            "끝난 뒤 Question Agent 가 채우며, 저장 계약의 `question` 으로 나간다. "
            "Timeline Agent 의 LLM 출력 계약에는 없다 — Timeline 이 쓰는 값이 아니다."
        ),
    )


# Backward-compatible export name used by existing imports.
TimelineDraftEvent = TimelineEventDraft


class TimelineAgentOutput(CamelModel):
    """Timeline Agent 의 LLM 출력 계약(#118).

    ``warnings`` 는 Timeline 만 아는 판단 — 근거가 충돌해 한쪽을 고른 곳, 일부러 쓰지
    않은 근거와 그 이유 — 을 담는다. Agent 실패·sourceRefs 누락·민감정보처럼 코드가
    이미 남기는 것은 여기 적을 이유가 없다.
    """

    events: list[TimelineAgentEvent] = Field(default_factory=list)
    warnings: list[TimelineWarning] = Field(default_factory=list)


class TimelineDraft(CamelModel):
    """Top-level daily timeline draft shown to the user for review."""

    user_id: str = Field(alias="userId", min_length=1)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone: str = Field(min_length=1)
    events: list[TimelineEventDraft] = Field(default_factory=list)
    warnings: list[TimelineWarning] = Field(default_factory=list)
