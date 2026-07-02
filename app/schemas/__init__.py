"""AI 하루 타임라인 입력 Source Item 스키마 패키지.

이슈 #7 — AI 하루 타임라인 입력 Source Item 계약 정의.
요청 DTO 는 `TimelineDraftRequest` 이며, 각 도메인 모델은 하위 모듈에 있다.
"""

from app.schemas.calendar import CalendarData, CalendarEvent
from app.schemas.common import (
    CamelModel,
    CollectionMode,
    GeoPoint,
    SourceItem,
    SourceType,
    TimeRange,
)
from app.schemas.event_candidate import (
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
)
from app.schemas.health import (
    ActiveCaloriesData,
    DistanceData,
    HealthData,
    HeartRateData,
    HeartRateSample,
    SleepData,
    StepsData,
    TotalCaloriesData,
)
from app.schemas.location import LocationData, LocationRoute, LocationStay
from app.schemas.notification import NotificationData, NotificationItem
from app.schemas.photo import PhotoItem
from app.schemas.timeline_request import TimelineDraftRequest, TimeWindow
from app.schemas.user_memory import UserMemory

__all__ = [
    "ActiveCaloriesData",
    "AiEventCandidate",
    "CalendarData",
    "CalendarEvent",
    "CamelModel",
    "CandidateTimeRange",
    "CollectionMode",
    "DistanceData",
    "EventSourceType",
    "EventType",
    "GeoPoint",
    "HealthData",
    "HeartRateData",
    "HeartRateSample",
    "InferenceLevel",
    "LocationData",
    "LocationRoute",
    "LocationStay",
    "NotificationData",
    "NotificationItem",
    "PhotoItem",
    "SleepData",
    "SourceItem",
    "SourceRef",
    "SourceType",
    "StepsData",
    "TimeRange",
    "TimeWindow",
    "TimelineDraftRequest",
    "TotalCaloriesData",
    "UserMemory",
]
