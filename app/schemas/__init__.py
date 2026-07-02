"""Laimory AI 서버 Pydantic 계약 패키지.

입력 Source Item, AI Event 후보, Agent 반환값 등 서버 내부/외부 계약을 모은다.
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
    AgentEventResult,
    AgentWarning,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceFragment,
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
from app.schemas.timeline import (
    TimelineDraft,
    TimelineDraftEvent,
    TimelineEventDraft,
    TimelineQuestion,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.schemas.timeline_request import TimelineDraftRequest, TimeWindow
from app.schemas.user_memory import UserMemory

__all__ = [
    "ActiveCaloriesData",
    "AgentEventResult",
    "AgentWarning",
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
    "SourceFragment",
    "SourceItem",
    "SourceRef",
    "SourceType",
    "StepsData",
    "TimeRange",
    "TimeWindow",
    "TimelineDraft",
    "TimelineDraftEvent",
    "TimelineDraftRequest",
    "TimelineEventDraft",
    "TimelineQuestion",
    "TimelineWarning",
    "TimelineWarningSeverity",
    "TotalCaloriesData",
    "UserMemory",
]
