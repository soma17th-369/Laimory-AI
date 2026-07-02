"""AI Event 후보 모델 스키마.

데이터별 Agent(위치·캘린더·사진·수면·활동·알림 등)가 자신의 source item 을
해석해 공통 형태로 반환하는 **AI Event 후보**(`AiEventCandidate`)를 정의한다.

주의: AI Event 후보는 **최종 timeline event 가 아니다.** 여러 Agent 가 내놓은
후보들을 Timeline Agent 가 병합·분할·검증해 최종 draft 를 만든다. 그래서 후보는
"어디까지 관측된 사실이고 어디부터 추론인지"를 스스로 밝혀 과한 추론을 막는다.

또한 후보에는 아직 **안정적인 `eventId` 가 없다.** 최종 `eventId` /
`clientEventId` 는 Timeline Agent 가 draft 를 만들 때 정해진다. run 내부 추적이
필요하면 DTO 본질 필드가 아니라 `candidateIndex` / `traceId` 같은 별도 수단으로
다룬다.

핵심 필드:
- `eventType`: 후보가 나타내는 활동 종류.
- `timeRange`: 후보가 걸쳐 있는 시간 구간(ISO 8601, timezone 포함).
- `title` / `description`: 사람이 읽을 요약과 설명.
- `sourceRefs`: 이 후보의 근거가 된 source item 참조 목록.
- `confidence`: 후보 확신도(0.0 ~ 1.0).
- `inferenceLevel`: 관측/근거 기반/추론/불확실 중 어느 수준인지.
- `uncertainty`: 불확실 요인 메모. 추론일수록 근거를 남기도록 유도한다.
"""

from enum import Enum

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import CamelModel


class EventType(str, Enum):
    """AI Event 후보 종류.

    UI 타입을 확정하는 것이 아니라, Timeline Agent 가 후보를 병합·해석하기 쉽게
    분류하는 것이 목적이다. 근거가 약하면 단정하지 말고 더 굵은 종류(예: `STAY`)나
    낮은 confidence 로 두고, 어디에도 맞지 않으면 `UNKNOWN` 으로 표기한다.
    """

    WAKE_UP = "WAKE_UP"  # 기상
    SLEEP = "SLEEP"  # 수면
    STAY = "STAY"  # 특정 장소 체류
    MOVEMENT = "MOVEMENT"  # 장소 간 이동
    CALENDAR_EVENT = "CALENDAR_EVENT"  # 캘린더 일정 존재
    MEAL = "MEAL"  # 식사
    PHOTO_MOMENT = "PHOTO_MOMENT"  # 사진으로 남긴 순간
    MEETING = "MEETING"  # 만남/미팅
    CLASS = "CLASS"  # 수업
    WORK = "WORK"  # 업무
    EXERCISE = "EXERCISE"  # 운동/신체 활동
    SOCIAL = "SOCIAL"  # 사교 활동
    REST = "REST"  # 휴식
    UNKNOWN = "UNKNOWN"  # 분류하기 어려운 후보


class EventSourceType(str, Enum):
    """AI Event 후보 근거의 source 종류.

    입력 Source Item 계약(이슈 #7)의 세분화된 `SourceType`(예: `location_stay`)이
    아니라, 후보가 근거로 삼은 데이터 도메인을 나타내는 **굵은 카테고리**다.
    `sourceId` 로 raw/derived 데이터를 역추적할 수 있게 짝을 이룬다.
    """

    LOCATION = "LOCATION"
    CALENDAR = "CALENDAR"
    PHOTO = "PHOTO"
    SLEEP = "SLEEP"
    ACTIVITY = "ACTIVITY"
    NOTIFICATION = "NOTIFICATION"
    USER_MEMORY = "USER_MEMORY"


class InferenceLevel(str, Enum):
    """후보가 만들어진 추론 수준.

    과한 추론을 막기 위한 기준이다. 값이 아래로 갈수록 해석 비중이 커지므로
    `confidence` 는 낮아지고 `uncertainty` 근거를 더 명확히 남겨야 한다.
    """

    DIRECT = "DIRECT"  # source 가 직접 말해주는 사실 (캘린더 일정 존재, 사진 촬영 시각, 수면 시작/종료)
    EVIDENCE_BASED = "EVIDENCE_BASED"  # 여러 근거를 조합한 판단 (식당 체류 + 음식 사진 → 식사 후보)
    INFERRED = "INFERRED"  # 근거는 있으나 해석 비중이 큰 판단 (GPS 속도·경로로 버스 이동 추정)
    UNCERTAIN = "UNCERTAIN"  # 근거가 약하거나 충돌해 확정하면 위험한 후보


class CandidateTimeRange(CamelModel):
    """AI Event 후보가 걸쳐 있는 시간 구간.

    입력 Source Item 의 epoch milliseconds 와 달리, 후보는 AI 출력이라
    가독성과 timezone 보존을 위해 ISO 8601(timezone 포함) datetime 을 쓴다.
    순간 이벤트(예: 사진)는 `startTime == endTime` 으로 둔다.
    """

    start_time: AwareDatetime = Field(alias="startTime")
    end_time: AwareDatetime = Field(alias="endTime")

    @model_validator(mode="after")
    def _validate_range(self) -> "CandidateTimeRange":
        if self.end_time < self.start_time:
            raise ValueError("endTime 은 startTime 이상이어야 합니다.")
        return self


class SourceRef(CamelModel):
    """AI Event 후보의 근거가 된 source 참조.

    - `sourceType`: 근거 데이터의 굵은 도메인 (`EventSourceType`).
    - `sourceId`: raw 또는 derived 데이터의 안정적 식별자. Reflection/Repair
      단계에서 "이 시간대의 이 source 들을 다시 분석하라"로 범위를 좁히는 핸들이다.
    """

    source_type: EventSourceType = Field(alias="sourceType")
    source_id: str = Field(alias="sourceId", min_length=1)


class AiEventCandidate(CamelModel):
    """데이터 Agent 가 반환하는 공통 AI Event 후보.

    안정적인 `eventId` 는 갖지 않는다. 최종 id 는 Timeline Agent 가 후보들을
    병합해 draft 를 만들 때 부여된다.
    """

    event_type: EventType = Field(alias="eventType")
    time_range: CandidateTimeRange = Field(alias="timeRange")
    title: str = Field(min_length=1, description="사람이 읽을 후보 제목")
    description: str = Field(description="후보 근거·판단에 대한 설명")
    source_refs: list[SourceRef] = Field(
        alias="sourceRefs",
        min_length=1,
        description="후보의 근거가 된 source 참조 (최소 1개)",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "후보 확신도. 0.85~1.00 강한 근거/여러 source 일치, "
            "0.65~0.84 근거 충분하나 해석 필요, 0.40~0.64 가능성은 있으나 확정 곤란, "
            "0.00~0.39 후보로만 보관/question·warning 대상."
        ),
    )
    inference_level: InferenceLevel = Field(alias="inferenceLevel")
    uncertainty: list[str] = Field(
        default_factory=list,
        description="불확실 요인 메모. 비어 있으면 특별한 불확실 요인이 없다는 뜻.",
    )

    @model_validator(mode="after")
    def _validate_inference(self) -> "AiEventCandidate":
        """과한 추론 방지 기준을 강제한다.

        근거가 약할수록(`UNCERTAIN`) 반드시 근거를 남기도록 `uncertainty` 를
        요구해, 설명 없는 낮은 신뢰 후보가 그대로 흘러가는 것을 막는다.
        """

        if self.inference_level is InferenceLevel.UNCERTAIN and not self.uncertainty:
            raise ValueError(
                "inferenceLevel 이 UNCERTAIN 인 후보는 uncertainty 근거를 최소 1개 남겨야 합니다."
            )
        return self


class SourceFragment(CamelModel):
    """event 로 확정하기엔 약하지만, 다른 데이터와 섞이면 event 가 될 수 있는 조각.

    데이터 Event Agent 가 자기 도메인에서 "혼자서는 event 가 아니지만 병합 재료가
    되는" source 를 `sourceId` + 내용 요약으로 남긴다.
    Timeline Agent 가 다른 source 후보와 시간·맥락으로 병합할 때 쓴다.
    """

    source_type: EventSourceType = Field(alias="sourceType")
    source_id: str = Field(alias="sourceId", min_length=1)
    summary: str = Field(min_length=1, description="조각 내용 요약")
    time_range: CandidateTimeRange | None = Field(
        default=None,
        alias="timeRange",
        description="병합에 쓰는 시간 정보. 시간 구간이 없으면 생략(None).",
    )


class AgentEventResult(CamelModel):
    """데이터 Event Agent 의 반환 계약.

    Agent 는 event 후보(`candidates`)만 반환하지 않는다. 혼자서는 event 로
    확정하기 약하지만 나중에 다른 데이터와 병합될 수 있는 source 단위 요약
    (`fragments`)을 함께 반환한다.
    """

    candidates: list[AiEventCandidate] = Field(default_factory=list)
    fragments: list[SourceFragment] = Field(default_factory=list)
