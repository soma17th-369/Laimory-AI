"""최종 타임라인 저장(_persist_timeline) 검증.

SQLite(in-memory)에 staging 테이블을 만들고, TimelineDraft 저장이
- 요청이 준 dailyRecordId 에 연결한 timeline_events INSERT,
- 근거 source 를 하루 1행으로 디듀프한 timeline_items INSERT,
- event↔item N:M 조인(timeline_event_items) INSERT,
- 재처리 시 기존 AI 결과 교체
를 보장하는지 확인한다.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.core.db_models import (
    DailyRecord,
    DraftSourceItem,
    TimelineEvent,
    TimelineEventItem,
    TimelineItem,
)
from app.schemas.event_candidate import EventType, InferenceLevel, SourceRef
from app.schemas.timeline import TimelineDraft, TimelineEventDraft
from app.services.timeline_repository import _persist_timeline
from app.services.timeline_validator import TimelineValidationError

# tzdata 없이도 도는 고정 +09:00(프로젝트의 KST 폴백과 동일 오프셋).
_KST = timezone(timedelta(hours=9))
_TASK = "t-1"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        yield sess
    await engine.dispose()


async def _seed_sources(session, user_id: int = 7) -> int:
    """daily record + source 3건을 시드하고 daily_record_id 를 돌려준다."""

    now = datetime(2026, 7, 8, 0, 0)
    daily_record = DailyRecord(
        user_id=user_id,
        record_date=date(2026, 7, 8),
        record_at=now,
        record_timezone="Asia/Seoul",
        status="READY",
        created_at=now,
        updated_at=now,
    )
    session.add_all(
        [
            daily_record,
            DraftSourceItem(
                task_id=_TASK, user_id=user_id, item_type="STAY", raw_id="raw-a",
                start_at=datetime(2026, 7, 8, 9, 0), end_at=datetime(2026, 7, 8, 10, 0),
                payload={"place": "카페"},
            ),
            DraftSourceItem(
                task_id=_TASK, user_id=user_id, item_type="PHOTO", raw_id="raw-b",
                start_at=datetime(2026, 7, 8, 9, 30), end_at=None, payload={},
            ),
            DraftSourceItem(
                task_id=_TASK, user_id=user_id, item_type="NOTIFICATION", raw_id="raw-c",
                start_at=datetime(2026, 7, 8, 12, 0), end_at=None, payload={},
            ),
        ]
    )
    await session.flush()
    return daily_record.daily_record_id


def _event(**kw) -> TimelineEventDraft:
    defaults = dict(
        client_event_id="e1",
        event_type=EventType.PHOTO_MOMENT,
        title="아침 산책",
        description="사진과 이동으로 추정",
        start_time=datetime(2026, 7, 8, 9, 0, tzinfo=_KST),
        end_time=datetime(2026, 7, 8, 10, 0, tzinfo=_KST),
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type="STAY", source_id="raw-a"),
            SourceRef(source_type="PHOTO", source_id="raw-b"),
        ],
    )
    defaults.update(kw)
    return TimelineEventDraft(**defaults)


def _draft(events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date="2026-07-08", timezone="Asia/Seoul", events=events
    )


async def _all(session, model):
    return list((await session.execute(select(model))).scalars().all())


async def test_insert_event_and_copy_used_sources(session):
    drid = await _seed_sources(session)

    saved = await _persist_timeline(session, _TASK, _draft([_event()]), drid)
    await session.commit()

    assert saved == 1
    events = await _all(session, TimelineEvent)
    assert len(events) == 1
    event = events[0]
    assert event.daily_record_id == drid
    assert event.title == "아침 산책"
    assert event.subtitle == "사진과 이동으로 추정"
    assert event.event_type == "PHOTO_MOMENT"
    # tz-aware KST → naive 벽시계
    assert event.start_at == datetime(2026, 7, 8, 9, 0)
    assert event.end_at == datetime(2026, 7, 8, 10, 0)

    items = await _all(session, TimelineItem)
    assert {item.raw_id for item in items} == {"raw-a", "raw-b"}
    stay_item = next(item for item in items if item.raw_id == "raw-a")
    assert stay_item.item_type == "STAY"
    assert stay_item.payload == {"place": "카페"}

    # 이벤트가 두 근거 item 과 N:M 조인으로 연결된다.
    joins = await _all(session, TimelineEventItem)
    assert len(joins) == 2
    assert all(join.timeline_event_id == event.timeline_event_id for join in joins)
    assert {join.timeline_item_id for join in joins} == {
        item.timeline_item_id for item in items
    }


async def test_reprocess_is_idempotent(session):
    drid = await _seed_sources(session)

    await _persist_timeline(session, _TASK, _draft([_event()]), drid)
    await session.commit()
    await _persist_timeline(session, _TASK, _draft([_event()]), drid)
    await session.commit()

    # 누적되지 않고 이벤트 1건 / item 2건 / 조인 2건만 남는다.
    assert len(await _all(session, TimelineEvent)) == 1
    assert len(await _all(session, TimelineItem)) == 2
    assert len(await _all(session, TimelineEventItem)) == 2


async def test_no_source_rows_raises(session):
    # source 를 시드하지 않으면 입력 검증에서 거부된다(FK 이전).
    with pytest.raises(ValueError):
        await _persist_timeline(session, _TASK, _draft([_event()]), 1)


async def test_mixed_source_users_are_rejected(session):
    drid = await _seed_sources(session)
    session.add(
        DraftSourceItem(
            task_id=_TASK,
            user_id=8,
            item_type="PHOTO",
            raw_id="raw-other-user",
            start_at=datetime(2026, 7, 8, 11, 0),
            payload={},
        )
    )
    await session.flush()

    with pytest.raises(ValueError, match="여러 user_id"):
        await _persist_timeline(session, _TASK, _draft([_event()]), drid)


async def test_validation_failure_writes_nothing(session):
    drid = await _seed_sources(session)

    invalid = _event(
        source_refs=[SourceRef(source_type="STAY", source_id="not-in-task")]
    )
    with pytest.raises(TimelineValidationError):
        await _persist_timeline(session, _TASK, _draft([invalid]), drid)
    await session.commit()

    # 검증은 어떤 쓰기보다 먼저라, 최종 event/item/조인이 남지 않는다.
    assert await _all(session, TimelineEvent) == []
    assert await _all(session, TimelineItem) == []
    assert await _all(session, TimelineEventItem) == []


async def test_same_source_shared_by_multiple_events(session):
    drid = await _seed_sources(session)
    first = _event(client_event_id="e1")
    second = _event(
        client_event_id="e2",
        title="후속 이벤트",
        source_refs=[SourceRef(source_type="STAY", source_id="raw-a")],
    )

    await _persist_timeline(session, _TASK, _draft([first, second]), drid)
    await session.commit()

    events = await _all(session, TimelineEvent)
    assert len(events) == 2

    # raw-a 는 하루 1행으로 디듀프되고, 두 이벤트가 같은 item 을 공유한다(N:M).
    raw_a_items = [item for item in await _all(session, TimelineItem) if item.raw_id == "raw-a"]
    assert len(raw_a_items) == 1
    raw_a_id = raw_a_items[0].timeline_item_id

    joins = await _all(session, TimelineEventItem)
    events_sharing_raw_a = {
        join.timeline_event_id for join in joins if join.timeline_item_id == raw_a_id
    }
    assert len(events_sharing_raw_a) == 2


async def test_reprocess_preserves_user_created_event(session):
    drid = await _seed_sources(session)
    now = datetime(2026, 7, 8, 8, 0)
    session.add(
        TimelineEvent(
            daily_record_id=drid,
            start_at=now,
            end_at=None,
            title="사용자 작성 이벤트",
            subtitle=None,
            memo=None,
            created_at=now,
            updated_at=now,
            modified_by="USER",
            event_type="UNKNOWN",
        )
    )
    await session.flush()

    await _persist_timeline(session, _TASK, _draft([_event()]), drid)
    await session.commit()

    events = await _all(session, TimelineEvent)
    assert {event.title for event in events} == {"사용자 작성 이벤트", "아침 산책"}
