"""AI 하루 타임라인 입력 요청 DTO.

하루 단위 라이프로그 raw snapshot 을 표현한다.

    기본 메타데이터
    + 위치 체류/이동 기록
    + 알림 수집 기록
    + 사진 메타데이터
    + 캘린더 일정
    + 건강 데이터
    + 사용자 메모리(보조 context)

`transactionId` 는 이 스냅샷 세트 전체를 식별하고, 각 source item 의
`sourceId` 는 세트 내에서 유니크하다.
"""

from pydantic import Field, model_validator

from app.schemas.calendar import CalendarData
from app.schemas.common import CamelModel, CollectionMode, SourceItem, TimestampMs
from app.schemas.health import HealthData
from app.schemas.location import LocationData
from app.schemas.notification import NotificationData
from app.schemas.photo import PhotoItem
from app.schemas.user_memory import UserMemory


class TimeWindow(CamelModel):
    """하루 수집 범위 (Unix epoch milliseconds)."""

    start: TimestampMs
    end: TimestampMs

    @model_validator(mode="after")
    def _validate_window(self) -> "TimeWindow":
        if self.end < self.start:
            raise ValueError("window.end 는 window.start 이상이어야 합니다.")
        return self


class TimelineDraftRequest(CamelModel):
    """타임라인 초안 생성 요청.

    AI 하루 타임라인 Agent 가 입력으로 받는 Source Item 계약이다.
    """

    transaction_id: str = Field(
        alias="transactionId",
        min_length=1,
        description="스냅샷 세트 전체를 식별하는 ID",
    )
    date: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="수집 대상 날짜 (YYYY-MM-DD)",
    )
    mode: CollectionMode
    window: TimeWindow
    generated_at: TimestampMs = Field(alias="generatedAt")

    location: LocationData = Field(default_factory=LocationData)
    notifications: NotificationData = Field(default_factory=NotificationData)
    photos: list[PhotoItem] = Field(default_factory=list)
    calendar: CalendarData = Field(default_factory=CalendarData)
    health: HealthData = Field(default_factory=HealthData)
    user_memory: UserMemory | None = Field(default=None, alias="userMemory")

    def iter_source_items(self) -> list[SourceItem]:
        """모든 도메인의 source item 을 하나의 리스트로 모은다.

        사용자 메모리는 비정형 보조 context 라 source item 집계에서 제외한다.
        """

        items: list[SourceItem] = []
        items.extend(self.location.stays)
        items.extend(self.location.routes)
        items.extend(self.notifications.active)
        items.extend(self.notifications.collected)
        items.extend(self.photos)
        items.extend(self.calendar.events)
        for health_item in (
            self.health.steps,
            self.health.sleep,
            self.health.total_calories_burned,
            self.health.active_calories_burned,
            self.health.distance,
            self.health.heart_rate,
        ):
            if health_item is not None:
                items.append(health_item)
        return items

    @model_validator(mode="after")
    def _validate_unique_source_ids(self) -> "TimelineDraftRequest":
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in self.iter_source_items():
            if item.source_id in seen:
                duplicates.add(item.source_id)
            seen.add(item.source_id)
        if duplicates:
            joined = ", ".join(sorted(duplicates))
            raise ValueError(f"sourceId 는 세트 내에서 유니크해야 합니다. 중복: {joined}")
        return self
