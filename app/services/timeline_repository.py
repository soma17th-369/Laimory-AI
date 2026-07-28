"""최종 타임라인 저장소.

Main Agent의 ``TimelineDraft``를 ``timeline_events`` / ``timeline_items`` /
``timeline_event_items``(N:M 조인)에 저장한다. 입력 ``timeline_draft_source_items``
는 조회만 하고 갱신하지 않는다.

이벤트가 속할 ``daily_record_id``는 요청(``POST /v1/timeline``)이 준다. AI 는
``daily_records``를 직접 조회/생성하지 않고, 받은 id 로 FK 만 건다. 같은
daily record 를 다시 처리할 때는 ``modified_by='AI'``인 기존 이벤트/아이템만
교체하고 사용자가 만든 것은 건드리지 않는다.

근거 source 는 저장 트랜잭션 단위로 디듀프한다. 같은 ``raw_id``는 ``timeline_items``
한 행으로만 저장하고, 여러 이벤트가 ``timeline_event_items`` 조인으로 그 item 을
공유한다. item 은 daily record 소속 같은 상위 개념을 직접 갖지 않고, 조인으로만
이벤트에 매달린다.
"""

from abc import ABC, abstractmethod
from datetime import datetime, tzinfo
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.db_models import (
    DraftSourceItem,
    TimelineEvent,
    TimelineEventItem,
    TimelineItem,
)
from app.core.logging import get_logger
from app.schemas.timeline import TimelineDraft, TimelineEventDraft
from app.services.source_repository import validate_source_rows
from app.services.timeline_validator import ensure_timeline_valid_for_storage
from app.services.validator import resolve_timezone

logger = get_logger(__name__)

_MODIFIED_BY = "AI"
_TITLE_MAX = 255
_SUBTITLE_MAX = 255
_EVENT_TYPE_MAX = 32


class TimelineRepository(ABC):
    """최종 타임라인 저장소 인터페이스."""

    @abstractmethod
    async def save(
        self, task_id: str, draft: TimelineDraft, daily_record_id: int
    ) -> int:
        """최종 이벤트와 근거 item을 저장하고 이벤트 수를 반환한다."""


class MySQLTimelineRepository(TimelineRepository):
    """MySQL ``timeline_events``/``timeline_items``/``timeline_event_items`` 저장 구현."""

    async def save(
        self, task_id: str, draft: TimelineDraft, daily_record_id: int
    ) -> int:
        async with session_scope() as session:
            saved = await _persist_timeline(session, task_id, draft, daily_record_id)
        logger.info(
            "최종 타임라인 저장 완료: taskId=%s, dailyRecordId=%s, events=%d",
            task_id,
            daily_record_id,
            saved,
        )
        return saved


class NoopTimelineRepository(TimelineRepository):
    """단위 테스트용 무동작 스텁. 실제 저장을 건너뛰고 0을 반환한다."""

    async def save(
        self, task_id: str, draft: TimelineDraft, daily_record_id: int
    ) -> int:
        logger.debug("무동작 저장소: 최종 타임라인 저장 건너뜀 (taskId=%s)", task_id)
        return 0


async def _persist_timeline(
    session: AsyncSession,
    task_id: str,
    draft: TimelineDraft,
    daily_record_id: int,
) -> int:
    """한 트랜잭션에서 기존 AI 결과를 교체하고 최종 타임라인을 저장한다."""

    source_result = await session.execute(
        select(DraftSourceItem).where(DraftSourceItem.task_id == task_id)
    )
    source_rows = list(source_result.scalars().all())
    # task/user/rawId/시각 일관성 검증(위반 시 예외). user_id 는 FK 에 쓰지 않는다.
    validate_source_rows(task_id, source_rows)
    sources_by_raw_id = {row.raw_id: row for row in source_rows}

    ensure_timeline_valid_for_storage(draft, set(sources_by_raw_id))

    await _replace_ai_rows(session, daily_record_id)

    tz = resolve_timezone(draft.timezone)
    now = datetime.now().replace(tzinfo=None)

    # 1) 사용된 근거 source 를 이번 저장 안에서 raw_id 1행으로 디듀프해 저장.
    item_id_by_raw = await _persist_items(session, draft, sources_by_raw_id, now)

    # 2) 이벤트 저장 + event↔item N:M 조인.
    for event in draft.events:
        timeline_event = TimelineEvent(
            daily_record_id=daily_record_id,
            start_at=_naive_local(event.start_time, tz),
            end_at=_naive_local(event.end_time, tz),
            title=event.title[:_TITLE_MAX],
            subtitle=_subtitle(event),
            memo=None,
            created_at=now,
            updated_at=now,
            modified_by=_MODIFIED_BY,
            event_type=event.event_type.value[:_EVENT_TYPE_MAX],
        )
        session.add(timeline_event)
        await session.flush()

        linked: set[str] = set()
        for ref in event.source_refs:
            if ref.raw_id in linked:
                continue
            linked.add(ref.raw_id)
            session.add(
                TimelineEventItem(
                    timeline_event_id=timeline_event.timeline_event_id,
                    timeline_item_id=item_id_by_raw[ref.raw_id],
                )
            )

    return len(draft.events)


async def _replace_ai_rows(session: AsyncSession, daily_record_id: int) -> None:
    """이 daily record 의 AI 생성 이벤트/조인/아이템을 지운다(멱등 재처리).

    item 은 daily record 를 직접 모르고 조인으로만 이벤트에 매달리므로, 교체할
    AI 이벤트가 참조하던 item 을 조인에서 찾아 지운다. 사용자가 만든 행
    (``modified_by != 'AI'``)은 보존한다. SQLite 는 기본적으로 FK cascade 를
    강제하지 않으므로 조인 → 이벤트 → 아이템 순으로 명시 삭제한다.
    """

    old_event_ids = list(
        (
            await session.execute(
                select(TimelineEvent.timeline_event_id).where(
                    TimelineEvent.daily_record_id == daily_record_id,
                    TimelineEvent.modified_by == _MODIFIED_BY,
                )
            )
        ).scalars().all()
    )
    if not old_event_ids:
        return

    old_item_ids = {
        item_id
        for item_id in (
            await session.execute(
                select(TimelineEventItem.timeline_item_id).where(
                    TimelineEventItem.timeline_event_id.in_(old_event_ids)
                )
            )
        ).scalars().all()
    }
    await session.execute(
        delete(TimelineEventItem).where(
            TimelineEventItem.timeline_event_id.in_(old_event_ids)
        )
    )
    await session.execute(
        delete(TimelineEvent).where(
            TimelineEvent.timeline_event_id.in_(old_event_ids)
        )
    )
    if old_item_ids:
        await session.execute(
            delete(TimelineItem).where(
                TimelineItem.timeline_item_id.in_(old_item_ids),
                TimelineItem.modified_by == _MODIFIED_BY,
            )
        )


async def _persist_items(
    session: AsyncSession,
    draft: TimelineDraft,
    sources_by_raw_id: dict[str, DraftSourceItem],
    now: datetime,
) -> dict[str, int]:
    """이벤트들이 참조한 raw_id 를 1행으로 저장하고 raw_id→item PK 맵을 만든다."""

    used_raw_ids: list[str] = []
    seen: set[str] = set()
    for event in draft.events:
        for ref in event.source_refs:
            if ref.raw_id not in seen:
                seen.add(ref.raw_id)
                used_raw_ids.append(ref.raw_id)

    item_id_by_raw: dict[str, int] = {}
    for raw_id in used_raw_ids:
        source = sources_by_raw_id[raw_id]
        item = TimelineItem(
            item_type=source.item_type,
            raw_id=source.raw_id,
            start_at=source.start_at,
            end_at=source.end_at,
            payload=source.payload or {},
            created_at=now,
            updated_at=now,
            modified_by=_MODIFIED_BY,
        )
        session.add(item)
        await session.flush()
        item_id_by_raw[raw_id] = item.timeline_item_id
    return item_id_by_raw


def _naive_local(value: datetime | None, tz: tzinfo) -> datetime | None:
    """aware datetime을 대상 timezone의 naive DB datetime으로 바꾼다."""

    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(tz)
    return value.replace(tzinfo=None)


def _subtitle(event: TimelineEventDraft) -> str | None:
    text = (event.description or "").strip()
    return text[:_SUBTITLE_MAX] if text else None


@lru_cache
def get_timeline_repository() -> TimelineRepository:
    """최종 타임라인 저장소 싱글턴을 반환한다.

    DB 는 필수이므로 항상 MySQL 저장소를 돌려준다. `NoopTimelineRepository` 는
    단위 테스트가 의존성 오버라이드로 직접 주입해 쓰는 무동작 스텁이다.
    """

    return MySQLTimelineRepository()
