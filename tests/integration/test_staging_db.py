"""실제 staging MySQL 을 대상으로 하는 opt-in 통합 테스트.

기본 실행에서는 접속 정보(DB_USER/DB_PASSWORD)가 없거나 접속이 안 되면(터널
미개통 등) skip 한다. 실행하려면 이슈 #25 스키마(timeline_events.daily_record_id +
timeline_event_items 복합 PK 조인, timeline_items 에서 event 직접 FK 제거)가
반영된 DB 여야 한다:

- SSH 터널을 열고(사설망 DB) `.env` 에 DB 접속 정보를 채운 뒤
- `uv run pytest -m integration tests/integration/test_staging_db.py`

쓰기 검증은 공유 데이터를 건드리지 않도록 합성 taskId/userId/date로만
daily record와 source를 seed/save하고, 테스트 전후로 그 흔적을 지운다(cleanup).
DB에서 직접 확인하려면 `KEEP_STAGING_TEST_DATA=true`로 실행해 종료 시 정리를
건너뛸 수 있다. 다음 실행 시작 시에는 기존 테스트 데이터를 먼저 정리한다.
"""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text

from app.core.config import settings
from app.core.db import get_engine, get_sessionmaker, session_scope
from app.core.db_models import (
    DailyRecord,
    DraftSourceItem,
    TimelineEvent,
    TimelineEventItem,
    TimelineItem,
)
from app.schemas.event_candidate import EventType, InferenceLevel, SourceRef
from app.schemas.timeline import TimelineDraft, TimelineEventDraft
from app.services.normalizer import normalize
from app.services.source_repository import MySQLSourceRepository
from app.services.timeline_repository import MySQLTimelineRepository

pytestmark = pytest.mark.integration

_KST = timezone(timedelta(hours=9))
# 공유 스테이징에서 실제 데이터와 겹치지 않을 합성 taskId.
_TASK = "__ai_it_smoke__"
_USER_ID = 999_999
_RAW_IDS = {
    "stay": "00000000-0000-4000-8000-000000000001",
    "movement": "00000000-0000-4000-8000-000000000002",
    "calendar": "00000000-0000-4000-8000-000000000003",
    "steps": "00000000-0000-4000-8000-000000000004",
    "sleep": "00000000-0000-4000-8000-000000000005",
    "notification": "00000000-0000-4000-8000-000000000006",
    "photo": "00000000-0000-4000-8000-000000000007",
}
_KEEP_TEST_DATA = os.getenv("KEEP_STAGING_TEST_DATA", "").lower() in {
    "1",
    "true",
    "yes",
}


async def _purge(task_id: str) -> None:
    async with session_scope() as session:
        daily_record_ids = list(
            (
                await session.execute(
                    select(DailyRecord.daily_record_id).where(
                        DailyRecord.user_id == _USER_ID,
                        DailyRecord.record_date == date(2026, 7, 8),
                    )
                )
            ).scalars().all()
        )
        event_ids = []
        if daily_record_ids:
            event_ids = list(
                (
                    await session.execute(
                        select(TimelineEvent.timeline_event_id).where(
                            TimelineEvent.daily_record_id.in_(daily_record_ids)
                        )
                    )
                ).scalars().all()
            )
        item_ids = []
        if event_ids:
            item_ids = list(
                (
                    await session.execute(
                        select(TimelineEventItem.timeline_item_id).where(
                            TimelineEventItem.timeline_event_id.in_(event_ids)
                        )
                    )
                ).scalars().all()
            )
            await session.execute(
                delete(TimelineEventItem).where(
                    TimelineEventItem.timeline_event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(TimelineEvent).where(TimelineEvent.timeline_event_id.in_(event_ids))
            )
        if item_ids:
            await session.execute(
                delete(TimelineItem).where(TimelineItem.timeline_item_id.in_(item_ids))
            )
        await session.execute(
            delete(DraftSourceItem).where(DraftSourceItem.task_id == task_id)
        )
        if daily_record_ids:
            await session.execute(
                delete(DailyRecord).where(DailyRecord.daily_record_id.in_(daily_record_ids))
            )


@pytest.fixture
async def require_db():
    if not settings.db_user or not settings.db_password:
        pytest.skip(
            "DB 접속 정보(DB_USER/DB_PASSWORD) 미설정: staging DB 통합 테스트를 건너뜁니다."
        )
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 접속 불가면 실패가 아니라 skip
        await _reset_db_engine()
        pytest.skip(f"staging DB 접속 불가(터널/.env 확인): {exc}")
    try:
        yield
    finally:
        # pytest가 테스트마다 새 event loop를 만들 수 있으므로, 이전 loop에 묶인
        # aiomysql pool을 다음 테스트가 재사용하지 않게 정리한다.
        await _reset_db_engine()


async def _reset_db_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


async def test_connectivity_and_tables(require_db):
    # 접속과 staging 테이블 존재를 확인한다(읽기 전용).
    async with session_scope() as session:
        await session.execute(select(DraftSourceItem).limit(1))
        await session.execute(select(DailyRecord).limit(1))
        await session.execute(select(TimelineEvent).limit(1))
        await session.execute(select(TimelineItem).limit(1))
        await session.execute(select(TimelineEventItem).limit(1))


async def test_read_and_save_roundtrip(require_db):
    now = datetime(2026, 7, 8, 0, 0, 0)
    await _purge(_TASK)
    try:
        # 1) 합성 daily record + 실제 입력 계약 형태의 source 7건을 seed한다.
        #    created_at/updated_at 는 실제
        #    테이블에 기본값이 없어 명시적으로 넣는다). daily_record_id 는 요청이
        #    주는 값 역할을 하므로 seed 후 확보해 save 에 넘긴다.
        daily_record = DailyRecord(
            user_id=_USER_ID, record_date=date(2026, 7, 8), record_at=now,
            record_timezone="Asia/Seoul", status="READY",
            created_at=now, updated_at=now, modified_by="AI",
        )
        async with session_scope() as session:
            session.add_all(
                [
                    daily_record,
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="STAY",
                        raw_id=_RAW_IDS["stay"],
                        start_at=datetime(2026, 7, 8, 9, 0), end_at=datetime(2026, 7, 8, 10, 0),
                        payload={
                            "latitude": 37.153867,
                            "longitude": 127.0782359,
                            "place": "오산운암3단지 주공아파트",
                            "address": "경기도 오산시 운암로 90",
                        },
                        created_at=now, updated_at=now,
                    ),
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="MOVEMENT",
                        raw_id=_RAW_IDS["movement"],
                        start_at=datetime(2026, 7, 8, 10, 0),
                        end_at=datetime(2026, 7, 8, 10, 20),
                        payload={
                            "start": {
                                "latitude": 37.153867,
                                "longitude": 127.0782359,
                                "place": "오산운암3단지 주공아파트",
                                "address": "경기도 오산시 운암로 90",
                            },
                            "end": {
                                "latitude": 37.1498,
                                "longitude": 127.0772,
                                "place": "오산역",
                                "address": "경기도 오산시 오산동",
                            },
                            "distanceMeters": 1870.04,
                            "transports": "WALKING",
                        },
                        created_at=now, updated_at=now,
                    ),
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="CALENDAR",
                        raw_id=_RAW_IDS["calendar"],
                        start_at=datetime(2026, 7, 8, 9, 0),
                        end_at=datetime(2026, 7, 8, 12, 0),
                        payload={
                            "title": "ASM 프로젝트 MVP 개발",
                            "description": "타임라인 기능 개발",
                            "locationText": "집(경기도 오산시 운암로 90)",
                            "allDay": False,
                        },
                        created_at=now, updated_at=now,
                    ),
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="HEALTH",
                        raw_id=_RAW_IDS["steps"],
                        start_at=datetime(2026, 7, 8, 0, 0),
                        end_at=datetime(2026, 7, 9, 0, 0),
                        payload={"metric": "STEPS", "value": "10631보"},
                        created_at=now, updated_at=now,
                    ),
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="HEALTH",
                        raw_id=_RAW_IDS["sleep"],
                        start_at=datetime(2026, 7, 8, 1, 10),
                        end_at=datetime(2026, 7, 8, 6, 50),
                        payload={"metric": "SLEEP", "value": "340분"},
                        created_at=now, updated_at=now,
                    ),
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="NOTIFICATION",
                        raw_id=_RAW_IDS["notification"],
                        start_at=datetime(2026, 7, 8, 12, 0), end_at=None,
                        payload={
                            "appName": "카카오톡",
                            "title": "프로젝트 팀 채팅",
                            "text": "오늘 오후 회의는 2시에 시작합니다.",
                        },
                        created_at=now, updated_at=now,
                    ),
                    DraftSourceItem(
                        task_id=_TASK, user_id=_USER_ID, item_type="PHOTO",
                        raw_id=_RAW_IDS["photo"],
                        start_at=datetime(2026, 7, 8, 9, 30), end_at=None,
                        payload={
                            "fileName": "20260708_093000.jpg",
                            "photoFile": "001_20260708_093000.jpg",
                            "latitude": 37.153867,
                            "longitude": 127.0782359,
                            "description": "노트북으로 프로젝트를 개발하는 모습",
                        },
                        created_at=now, updated_at=now,
                    ),
                ]
            )
        drid = daily_record.daily_record_id

        # 2) MySQLSourceRepository 로 읽어 CollectedSnapshot 조립 확인.
        snapshot = await MySQLSourceRepository().get(_TASK)
        assert snapshot is not None
        assert {item.raw_id for item in snapshot.source_items} == set(_RAW_IDS.values())

        # DB에서 읽은 payload가 실제 normalizer 입력 계약을 모두 통과하는지 확인한다.
        request = normalize(snapshot)
        assert len(request.stays) == 1
        assert len(request.movements) == 1
        assert len(request.calendars) == 1
        assert len(request.healths) == 2
        assert len(request.notifications) == 1
        assert len(request.photos) == 1
        assert request.stays[0].place == "오산운암3단지 주공아파트"
        assert request.movements[0].transports == ["WALKING"]
        assert request.healths[0].value == 10631
        assert request.healths[1].duration_minutes == 340
        assert request.photos[0].client_photo_uri == "001_20260708_093000.jpg"

        # 3) 이벤트 1건(STAY, PHOTO 사용)을 저장.
        draft = TimelineDraft(
            user_id="u", date="2026-07-08", timezone="Asia/Seoul",
            events=[
                TimelineEventDraft(
                    client_event_id="e1", event_type=EventType.PHOTO_MOMENT,
                    title="통합 스모크 이벤트", description="it-a + it-b",
                    start_time=datetime(2026, 7, 8, 9, 0, tzinfo=_KST),
                    end_time=datetime(2026, 7, 8, 10, 0, tzinfo=_KST),
                    confidence=0.8, inference_level=InferenceLevel.EVIDENCE_BASED,
                    source_refs=[
                        SourceRef(source_type="STAY", source_id=_RAW_IDS["stay"]),
                        SourceRef(source_type="PHOTO", source_id=_RAW_IDS["photo"]),
                    ],
                )
            ],
        )
        saved = await MySQLTimelineRepository().save(_TASK, draft, drid)
        assert saved == 1

        # 4) timeline event 1건 + 하루 단위 item 2건 + N:M 조인 2건 확인.
        async with session_scope() as session:
            events = list(
                (
                    await session.execute(
                        select(TimelineEvent).where(
                            TimelineEvent.daily_record_id == drid,
                            TimelineEvent.modified_by == "AI",
                        )
                    )
                ).scalars().all()
            )
            assert len(events) == 1
            event = events[0]
            assert event.title == "통합 스모크 이벤트"
            assert event.event_type == "PHOTO_MOMENT"

            # item 은 조인을 통해서만 이벤트에 매달린다.
            joins = list(
                (
                    await session.execute(
                        select(TimelineEventItem).where(
                            TimelineEventItem.timeline_event_id
                            == event.timeline_event_id
                        )
                    )
                ).scalars().all()
            )
            assert len(joins) == 2

            items = list(
                (
                    await session.execute(
                        select(TimelineItem).where(
                            TimelineItem.timeline_item_id.in_(
                                [join.timeline_item_id for join in joins]
                            )
                        )
                    )
                ).scalars().all()
            )
            assert {item.raw_id for item in items} == {
                _RAW_IDS["stay"],
                _RAW_IDS["photo"],
            }

        # 5) 재저장 멱등: 여전히 1건.
        await MySQLTimelineRepository().save(_TASK, draft, drid)
        async with session_scope() as session:
            count = list(
                (
                    await session.execute(
                        select(TimelineEvent).where(
                            TimelineEvent.daily_record_id == drid,
                            TimelineEvent.modified_by == "AI",
                        )
                    )
                ).scalars().all()
            )
            assert len(count) == 1
    finally:
        if _KEEP_TEST_DATA:
            print(
                "\n테스트 데이터를 유지합니다: "
                f"taskId={_TASK}, userId={_USER_ID}, date=2026-07-08"
            )
        else:
            await _purge(_TASK)
