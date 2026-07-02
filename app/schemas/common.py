"""타임라인 입력 Source Item 공통 스키마.

모든 source item 이 공유하는 공통 필드(`sourceId`, `sourceType`)와
열거형(수집 모드, source type), 재사용 타입(좌표·시각)을 정의한다.
각 도메인별 payload 모델은 이 모듈의 `SourceItem` 을 상속해 공통 필드를
물려받는다.
"""

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CamelModel(BaseModel):
    """프로젝트 공통 base 모델.

    요청 JSON 은 camelCase(예: `sourceId`, `startTime`)로 들어오므로
    alias 를 통해 매핑하고, 파이썬 코드에서는 snake_case 필드명으로도
    값을 채울 수 있도록 `populate_by_name` 을 켠다.
    """

    model_config = ConfigDict(populate_by_name=True)


# Unix epoch milliseconds. 음수는 유효하지 않은 시각으로 본다.
TimestampMs = Annotated[int, Field(ge=0, description="Unix epoch milliseconds")]

# 위경도 좌표. WGS84 유효 범위로 제한한다.
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class CollectionMode(str, Enum):
    """하루 데이터 수집 모드.

    새로운 수집 모드가 생기면 여기에 값을 추가한다.
    """

    FULL_DAY = "FULL_DAY"


class SourceType(str, Enum):
    """source item 종류 식별자.

    각 payload 모델이 자신의 `sourceType` 을 고정값으로 선언해,
    AI 결과 검증과 이후 선택적 재처리 시 item 종류를 구분하는 데 쓴다.
    """

    LOCATION_STAY = "location_stay"
    LOCATION_ROUTE = "location_route"
    NOTIFICATION = "notification"
    PHOTO = "photo"
    CALENDAR_EVENT = "calendar_event"
    HEALTH_STEPS = "health_steps"
    HEALTH_SLEEP = "health_sleep"
    HEALTH_TOTAL_CALORIES = "health_total_calories"
    HEALTH_ACTIVE_CALORIES = "health_active_calories"
    HEALTH_DISTANCE = "health_distance"
    HEALTH_HEART_RATE = "health_heart_rate"


class GeoPoint(CamelModel):
    """위경도 좌표 한 점."""

    lat: Latitude
    lon: Longitude


class SourceItem(CamelModel):
    """모든 source item 공통 필드.

    - `sourceId`: 수집 주체가 전달하는 안정적인 식별자.
      AI 결과 검증과 이후 선택적 재처리의 기준 키가 된다.
    - `sourceType`: item 종류. 각 payload 모델이 고정값으로 override 한다.
    """

    source_id: str = Field(alias="sourceId", min_length=1)
    source_type: SourceType = Field(alias="sourceType")


class TimeRange(CamelModel):
    """시작/종료 시각을 갖는 항목 공통 필드.

    체류·이동·수면·일정처럼 구간을 갖는 source item 이 상속한다.
    """

    start_time: TimestampMs = Field(alias="startTime")
    end_time: TimestampMs = Field(alias="endTime")

    @model_validator(mode="after")
    def _validate_time_range(self) -> "TimeRange":
        if self.end_time < self.start_time:
            raise ValueError("endTime 은 startTime 이상이어야 합니다.")
        return self
