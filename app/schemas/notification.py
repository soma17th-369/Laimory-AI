"""알림 source item 스키마.

현재 활성 알림(`active`)과 하루 동안 수집된 알림(`collected`)을 정의한다.
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel, SourceItem, SourceType, TimestampMs


class NotificationItem(SourceItem):
    """알림 한 건."""

    source_type: Literal[SourceType.NOTIFICATION] = Field(
        SourceType.NOTIFICATION, alias="sourceType"
    )
    package_name: str = Field(alias="packageName")
    app_name: str = Field(alias="appName")
    title: str
    text: str
    post_time: TimestampMs = Field(alias="postTime")
    collected_at: TimestampMs = Field(alias="collectedAt")


class NotificationData(CamelModel):
    """알림 도메인 컨테이너."""

    active: list[NotificationItem] = Field(default_factory=list)
    collected: list[NotificationItem] = Field(default_factory=list)
