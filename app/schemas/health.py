"""건강 source item 스키마.

걸음 수, 수면, 소모 칼로리(총/활동), 이동 거리, 심박 샘플을 정의한다.
각 지표는 하루 동안 수집되지 않았을 수 있어 컨테이너에서 선택 값으로 둔다.
수면·활동 관련 데이터는 이슈의 입력 타입(수면, 활동)에 해당한다.
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import (
    CamelModel,
    SourceItem,
    SourceType,
    TimeRange,
    TimestampMs,
)


class StepsData(SourceItem):
    """걸음 수."""

    source_type: Literal[SourceType.HEALTH_STEPS] = Field(
        SourceType.HEALTH_STEPS, alias="sourceType"
    )
    count: int = Field(ge=0)


class SleepData(SourceItem, TimeRange):
    """수면 기록."""

    source_type: Literal[SourceType.HEALTH_SLEEP] = Field(
        SourceType.HEALTH_SLEEP, alias="sourceType"
    )
    duration_minutes: int = Field(alias="durationMinutes", ge=0)


class TotalCaloriesData(SourceItem):
    """총 소모 칼로리."""

    source_type: Literal[SourceType.HEALTH_TOTAL_CALORIES] = Field(
        SourceType.HEALTH_TOTAL_CALORIES, alias="sourceType"
    )
    kcal: float = Field(ge=0)


class ActiveCaloriesData(SourceItem):
    """활동 소모 칼로리."""

    source_type: Literal[SourceType.HEALTH_ACTIVE_CALORIES] = Field(
        SourceType.HEALTH_ACTIVE_CALORIES, alias="sourceType"
    )
    kcal: float = Field(ge=0)


class DistanceData(SourceItem):
    """이동 거리."""

    source_type: Literal[SourceType.HEALTH_DISTANCE] = Field(
        SourceType.HEALTH_DISTANCE, alias="sourceType"
    )
    meters: float = Field(ge=0)


class HeartRateSample(CamelModel):
    """심박 샘플 한 점."""

    time: TimestampMs
    bpm: int = Field(ge=0)


class HeartRateData(SourceItem):
    """심박 데이터.

    개별 샘플은 `samples` payload 로 담고, 이 블록 전체가 하나의 source item 이다.
    """

    source_type: Literal[SourceType.HEALTH_HEART_RATE] = Field(
        SourceType.HEALTH_HEART_RATE, alias="sourceType"
    )
    samples: list[HeartRateSample] = Field(default_factory=list)


class HealthData(CamelModel):
    """건강 도메인 컨테이너."""

    steps: StepsData | None = None
    sleep: SleepData | None = None
    total_calories_burned: TotalCaloriesData | None = Field(
        default=None, alias="totalCaloriesBurned"
    )
    active_calories_burned: ActiveCaloriesData | None = Field(
        default=None, alias="activeCaloriesBurned"
    )
    distance: DistanceData | None = None
    heart_rate: HeartRateData | None = Field(default=None, alias="heartRate")
