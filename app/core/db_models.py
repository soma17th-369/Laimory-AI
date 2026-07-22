"""staging RDB 테이블 ORM 매핑.

App Server 가 이미 정의·운영하는 테이블을 그대로 매핑한다(테이블 생성은
App Server 책임이며 AI 서버는 만들지 않는다).

- ``timeline_draft_source_items``: AI 분석 입력. ``taskId`` 로 조회만 한다.
- ``daily_records``: 최종 타임라인이 속할 사용자/날짜 단위 기록. AI 는 요청으로 받은
  ``dailyRecordId`` 로 이벤트를 연결만 하고, 이 테이블을 직접 조회/생성하지 않는다.
- ``timeline_events``: Main Agent 가 확정한 최종 이벤트.
- ``timeline_items``: 이벤트 근거 source 의 스냅샷. 이벤트에 직접 매달리지 않고
  ``daily_record_id`` 로 하루 단위에 속하며, 같은 날 같은 ``raw_id`` 는 한 행으로
  디듀프한다.
- ``timeline_event_items``: event ↔ item N:M 조인. 여러 이벤트가 같은 item 을 공유한다.

단위 테스트에서는 같은 매핑으로 SQLite 테이블을 생성해 저장 계약을 검증한다.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# BigInteger PK 는 SQLite 에서 autoincrement 되지 않는다(INTEGER PK 만 rowid 로
# 자동 증가). 단위 테스트(SQLite)에서 INSERT 가 되도록, PK 는 SQLite 에서만
# INTEGER 로 렌더링한다. MySQL 에서는 BIGINT AUTO_INCREMENT 그대로다.
_AutoPk = BigInteger().with_variant(Integer, "sqlite")


class DraftSourceItem(Base):
    """``timeline_draft_source_items`` 한 행(입력 source item)."""

    __tablename__ = "timeline_draft_source_items"

    timeline_draft_source_item_id: Mapped[int] = mapped_column(
        _AutoPk, primary_key=True, autoincrement=True
    )
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_id: Mapped[str] = mapped_column(String(36), nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )
    modified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DailyRecord(Base):
    """``daily_records`` 사용자/날짜 단위 기록."""

    __tablename__ = "daily_records"

    daily_record_id: Mapped[int] = mapped_column(
        _AutoPk, primary_key=True, autoincrement=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    record_date: Mapped[date] = mapped_column(Date, nullable=False)
    record_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    record_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    emotion_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    modified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TimelineEvent(Base):
    """``timeline_events``에 저장하는 최종 타임라인 이벤트."""

    __tablename__ = "timeline_events"

    timeline_event_id: Mapped[int] = mapped_column(
        _AutoPk, primary_key=True, autoincrement=True
    )
    daily_record_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("daily_records.daily_record_id", ondelete="CASCADE"),
        nullable=False,
    )
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    modified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="UNKNOWN"
    )


class TimelineItem(Base):
    """``timeline_items``에 저장하는 이벤트 근거 source 스냅샷.

    이벤트에 직접 매달리지 않고(과거 ``timeline_event_id`` FK 제거) ``timeline_event_items``
    조인으로만 이벤트와 연결된다. daily record 소속 같은 상위 개념은 이벤트가 갖고
    item 은 갖지 않는다. 한 저장 트랜잭션 안에서 같은 ``raw_id`` 는 한 행으로만
    만들어 여러 이벤트가 공유한다(N:M).
    """

    __tablename__ = "timeline_items"

    timeline_item_id: Mapped[int] = mapped_column(
        _AutoPk, primary_key=True, autoincrement=True
    )
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_id: Mapped[str] = mapped_column(String(36), nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    modified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TimelineEventItem(Base):
    """``timeline_event_items`` event ↔ item N:M 조인 한 행.

    App Server 가 만든 실제 테이블은 서로게이트 PK/타임스탬프 없이
    ``(timeline_event_id, timeline_item_id)`` 를 복합 기본키로 갖는 순수 조인
    테이블이다. 한 이벤트가 여러 근거 item 을, 한 item 이 여러 이벤트를 근거로
    가질 수 있고, 복합 PK 가 그 쌍의 유일성을 보장한다.
    """

    __tablename__ = "timeline_event_items"

    timeline_event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("timeline_events.timeline_event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    timeline_item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("timeline_items.timeline_item_id", ondelete="CASCADE"),
        primary_key=True,
    )
