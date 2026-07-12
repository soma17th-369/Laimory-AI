"""AI Event 후보 모델 스키마.

데이터별 Event Agent가 자기 source를 해석해 Timeline Agent에 넘기는 중간 계약이다.
``candidates``는 event로 볼 근거가 충분한 후보이고, ``fragments``는 후보보다
불확실하지만 하루 event일 가능성이 있는 단서다.
"""

from enum import Enum

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from app.schemas.common import CamelModel


class EventType(str, Enum):
    """AI Event 후보 종류."""

    WAKE_UP = "WAKE_UP"
    SLEEP = "SLEEP"
    MOVEMENT = "MOVEMENT"
    CALENDAR_EVENT = "CALENDAR_EVENT"
    MEAL = "MEAL"
    PHOTO_MOMENT = "PHOTO_MOMENT"
    MEETING = "MEETING"
    CLASS = "CLASS"
    WORK = "WORK"
    EXERCISE = "EXERCISE"
    SOCIAL = "SOCIAL"
    REST = "REST"
    UNKNOWN = "UNKNOWN"


class EventSourceType(str, Enum):
    """AI Event 후보가 참조하는 근거 source 도메인."""

    STAY = "STAY"
    MOVEMENT = "MOVEMENT"
    CALENDAR = "CALENDAR"
    PHOTO = "PHOTO"
    SLEEP = "SLEEP"
    ACTIVITY = "ACTIVITY"
    NOTIFICATION = "NOTIFICATION"
    USER_MEMORY = "USER_MEMORY"


class InferenceLevel(str, Enum):
    """후보가 만들어진 추론 수준."""

    DIRECT = "DIRECT"
    EVIDENCE_BASED = "EVIDENCE_BASED"
    INFERRED = "INFERRED"
    UNCERTAIN = "UNCERTAIN"


class CandidateTimeRange(CamelModel):
    """AI Event 후보 또는 단서가 걸쳐 있는 시간 구간."""

    start_time: AwareDatetime = Field(alias="startTime")
    end_time: AwareDatetime = Field(alias="endTime")

    @model_validator(mode="after")
    def _validate_range(self) -> "CandidateTimeRange":
        if self.end_time < self.start_time:
            raise ValueError("endTime must be greater than or equal to startTime")
        return self


class SourceRef(CamelModel):
    """후보 또는 draft event의 근거 source 참조."""

    source_type: EventSourceType = Field(alias="sourceType")
    source_id: str = Field(
        alias="rawId",
        validation_alias=AliasChoices("rawId", "sourceId"),
        min_length=1,
    )
    reason: str | None = Field(
        default=None,
        description="이 source가 후보 또는 draft event의 근거가 되는 이유",
    )


class AiEventCandidate(CamelModel):
    """데이터별 Event Agent가 반환하는 AI Event 후보.

    후보는 최종 timeline event가 아니다. Timeline Agent가 여러 후보와 단서를 병합하고
    검토해 최종 draft event를 만든다.
    """

    event_type: EventType = Field(alias="eventType")
    time_range: CandidateTimeRange = Field(alias="timeRange")
    title: str = Field(min_length=1, description="사용자가 읽을 후보 제목")
    description: str = Field(description="후보 판단 근거와 해석")
    evidence_summary: str | None = Field(
        default=None,
        alias="evidenceSummary",
        description="Timeline Agent가 병합 판단에 참고할 핵심 근거 요약",
    )
    semantic_tags: list[str] = Field(
        default_factory=list,
        alias="semanticTags",
        description="사진 description 등에서 추출한 활동/장소/상황 태그",
    )
    source_refs: list[SourceRef] = Field(
        alias="sourceRefs",
        min_length=1,
        description="후보의 근거 source 참조 목록",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="후보 확신도. 근거가 약하면 보기 좋게 높이지 않는다.",
    )
    inference_level: InferenceLevel = Field(alias="inferenceLevel")
    uncertainty: list[str] = Field(
        default_factory=list,
        description="불확실한 이유. UNCERTAIN 후보는 최소 1개 이상 필요하다.",
    )

    @model_validator(mode="after")
    def _validate_inference(self) -> "AiEventCandidate":
        if self.inference_level is InferenceLevel.UNCERTAIN and not self.uncertainty:
            raise ValueError(
                "inferenceLevel is UNCERTAIN, but uncertainty is empty"
            )
        return self


class SourceFragment(CamelModel):
    """candidate보다 불확실하지만 하루 event일 가능성이 있는 추측/단서.

    SourceFragment는 원본 데이터를 그대로 요약하는 필드가 아니다. 데이터별 Event Agent가
    자기 도메인에서 "event일 수 있지만 아직 후보로 확정하기에는 근거가 약한" 단서를
    ``sourceId``와 짧은 해석으로 남긴다. Timeline Agent는 이 단서를 다른 후보와 함께
    비교해 question, warning, 낮은 confidence event, 또는 보강 근거로 활용한다.
    """

    source_type: EventSourceType = Field(alias="sourceType")
    source_id: str = Field(
        alias="sourceId",
        validation_alias=AliasChoices("sourceId", "rawId"),
        min_length=1,
    )
    summary: str = Field(
        min_length=1,
        description="candidate보다 불확실한 하루 event 단서에 대한 짧은 해석",
    )
    time_range: CandidateTimeRange | None = Field(
        default=None,
        alias="timeRange",
        description="단서가 걸쳐 있는 시간 정보. 시간 구간이 없으면 생략한다.",
    )


class AgentWarning(CamelModel):
    """Agent 실행 중 복구 가능한 실패나 누락을 전달하는 경고."""

    agent_name: str = Field(alias="agentName", min_length=1)
    message: str = Field(min_length=1)


class AgentEventResult(CamelModel):
    """데이터별 Event Agent 반환 계약."""

    candidates: list[AiEventCandidate] = Field(default_factory=list)
    fragments: list[SourceFragment] = Field(default_factory=list)
    warnings: list[AgentWarning] = Field(default_factory=list)
