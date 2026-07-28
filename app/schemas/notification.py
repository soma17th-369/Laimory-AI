"""알림 도메인 항목 스키마.

수집 스냅샷의 `itemType=NOTIFICATION` 을 분리한 뒤의 형태를 정의한다.
알림은 순간 이벤트라 `posted_at`(항목의 `startAt`) 하나만 시각으로 갖는다.
"""

from pydantic import Field

from app.schemas.common import CamelModel, RawId


class NotificationItem(CamelModel):
    """알림 한 건 (NOTIFICATION)."""

    raw_id: RawId = Field(alias="rawId")
    posted_at: str = Field(alias="postedAt")
    app_name: str = Field(alias="appName")
    title: str
    text: str
