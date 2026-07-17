"""정규화된 타임라인 초안 생성 요청 DTO.

`app/services/normalizer.py` 가 수집 스냅샷(`CollectedSnapshot`)의 평평한
`sourceItems` 를 `itemType` 기준으로 분리해 만든 결과이며, main agent 와 각
Event Agent 가 입력으로 소비한다.

`taskId` 가 이 처리 단위를 식별하고, 각 도메인 리스트는 해당 종류의 항목만
담는다. 시각은 ISO 문자열 그대로 유지한다.
"""

from pydantic import Field

from app.schemas.calendar import CalendarItem
from app.schemas.common import CamelModel
from app.schemas.health import HealthItem
from app.schemas.stay import MovementItem, StayItem
from app.schemas.notification import NotificationItem
from app.schemas.photo import PhotoItem
from app.schemas.user_memory import UserMemory


class TimeWindow(CamelModel):
    """타임라인 생성 대상 시간 창 (ISO 문자열)."""

    start: str
    end: str


class TimelineDraftRequest(CamelModel):
    """정규화된 타임라인 초안 생성 요청.

    수집 스냅샷을 도메인별로 분리한 뒤의 형태다. main agent 는 이 요청을 각
    Event Agent 에 넘겨 event 후보를 뽑고, Timeline Agent 로 병합한다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    transaction_id: str = Field(alias="transactionId", min_length=1)
    date: str = Field(description="수집 대상 날짜 (YYYY-MM-DD)")
    timezone: str = "Asia/Seoul"
    window: TimeWindow | None = None
    user_memory: UserMemory | None = Field(default=None, alias="userMemory")

    stays: list[StayItem] = Field(default_factory=list)
    movements: list[MovementItem] = Field(default_factory=list)
    calendars: list[CalendarItem] = Field(default_factory=list)
    healths: list[HealthItem] = Field(default_factory=list)
    notifications: list[NotificationItem] = Field(default_factory=list)
    photos: list[PhotoItem] = Field(default_factory=list)

    def iter_source_items(self) -> list:
        """모든 도메인 항목을 하나의 리스트로 모은다(로깅/집계용).

        userMemory 는 도메인 항목이 아니므로 포함하지 않는다.
        """

        return [
            *self.stays,
            *self.movements,
            *self.calendars,
            *self.healths,
            *self.notifications,
            *self.photos,
        ]
