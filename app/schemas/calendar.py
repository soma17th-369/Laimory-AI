"""캘린더 도메인 항목 스키마.

수집 스냅샷의 `itemType=CALENDAR` 를 분리한 뒤의 형태를 정의한다.
장소·설명은 비어 있을 수 있어 선택 값이다.
"""

from pydantic import Field

from app.schemas.common import CamelModel


class CalendarItem(CamelModel):
    """캘린더 일정 한 건 (CALENDAR)."""

    id: int
    raw_id: str | None = Field(default=None, alias="rawId")
    start_at: str = Field(alias="startAt")
    end_at: str | None = Field(default=None, alias="endAt")
    title: str
    description: str | None = None
    location_text: str | None = Field(default=None, alias="locationText")
    all_day: bool = Field(default=False, alias="allDay")
