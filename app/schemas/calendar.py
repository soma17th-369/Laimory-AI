"""캘린더 source item 스키마.

일정 제목, 시작/종료 시각, 장소, 설명을 정의한다.
장소와 설명은 비어 있을 수 있어 선택 값이다.
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel, SourceItem, SourceType, TimeRange


class CalendarEvent(SourceItem, TimeRange):
    """캘린더 일정 한 건."""

    source_type: Literal[SourceType.CALENDAR_EVENT] = Field(
        SourceType.CALENDAR_EVENT, alias="sourceType"
    )
    title: str
    location: str | None = None
    description: str | None = None


class CalendarData(CamelModel):
    """캘린더 도메인 컨테이너."""

    events: list[CalendarEvent] = Field(default_factory=list)
