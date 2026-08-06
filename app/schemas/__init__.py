"""Laimory AI 서버 Pydantic 계약 패키지.

수집 스냅샷(원본) 입력, 도메인별로 분리된 항목, AI Event 후보, Agent 반환값 등
서버 내부/외부 계약을 모은다.
"""

from app.schemas.calendar import CalendarItem
from app.schemas.common import CamelModel, GeoPlace, Latitude, Longitude
from app.schemas.error import ErrorResponse
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
from app.schemas.health import HealthItem
from app.schemas.stay import MovementItem, StayItem
from app.schemas.notification import NotificationItem
from app.schemas.photo import PhotoItem
from app.schemas.repair import (
    RepairIssue,
    RepairPlan,
    RepairToolCall,
    RepairToolResult,
)
from app.schemas.source_snapshot import (
    CollectedSnapshot,
    CollectedSourceItem,
    HealthMetric,
    ItemType,
    TimelineWindow,
)
from app.schemas.task import TaskStatus, TimelineCallbackPayload
from app.schemas.timeline import (
    TimelineDraft,
    TimelineDraftEvent,
    TimelineEventDraft,
    TimelineQuestion,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.schemas.timeline_input import TimelineInputResponse, TimelineInputWindow
from app.schemas.timeline_request import TimelineDraftRequest, TimeWindow
from app.schemas.timeline_result import TimelineResultEvent, TimelineResultRequest
from app.schemas.user_memory import UserMemory
from app.schemas.user_memory_update import (
    DailyTimeline,
    DailyTimelineEvent,
    UserMemoryResultRequest,
    UserMemoryUpdateRequest,
    UserMemoryUpdateResponse,
)

__all__ = [
    "AgentEventResult",
    "AgentWarning",
    "AiEventCandidate",
    "CalendarItem",
    "CamelModel",
    "CandidateTimeRange",
    "CollectedSnapshot",
    "CollectedSourceItem",
    "DailyTimeline",
    "DailyTimelineEvent",
    "ErrorResponse",
    "EventSourceType",
    "EventType",
    "GeoPlace",
    "HealthItem",
    "HealthMetric",
    "InferenceLevel",
    "ItemType",
    "Latitude",
    "Longitude",
    "MovementItem",
    "NotificationItem",
    "PhotoItem",
    "RepairIssue",
    "RepairPlan",
    "RepairToolCall",
    "RepairToolResult",
    "SourceFragment",
    "SourceRef",
    "StayItem",
    "TaskStatus",
    "TimeWindow",
    "TimelineCallbackPayload",
    "TimelineDraft",
    "TimelineDraftEvent",
    "TimelineDraftRequest",
    "TimelineEventDraft",
    "TimelineInputResponse",
    "TimelineInputWindow",
    "TimelineQuestion",
    "TimelineResultEvent",
    "TimelineResultRequest",
    "TimelineWarning",
    "TimelineWarningSeverity",
    "TimelineWindow",
    "UserMemory",
    "UserMemoryResultRequest",
    "UserMemoryUpdateRequest",
    "UserMemoryUpdateResponse",
]
