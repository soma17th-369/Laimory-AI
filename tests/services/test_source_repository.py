"""수집 스냅샷 저장소 검증.

InMemorySourceRepository 왕복과, MySQL 행 → CollectedSnapshot 조립(_rows_to_snapshot)
파생 규칙을 확인한다. 조립 테스트는 DB 없이 ORM 인스턴스로만 검증한다.
"""

from datetime import datetime
from pathlib import Path

import pytest

from app.core.db_models import DraftSourceItem
from app.services.source_repository import (
    InMemorySourceRepository,
    SourceBatchError,
    _rows_to_snapshot,
    load_snapshot_from_file,
)
from tests.fixtures.requests import default_source_items, make_snapshot

_SAMPLE = Path(__file__).resolve().parents[2] / "data" / "input" / "2026-07-08.json"


async def test_put_then_get_roundtrip():
    repo = InMemorySourceRepository()
    snapshot = make_snapshot(task_id="task-abc", source_items=default_source_items())
    repo.put(snapshot)

    got = await repo.get("task-abc")
    assert got is snapshot
    assert len(got.source_items) == 7


async def test_get_missing_returns_none():
    assert await InMemorySourceRepository().get("missing") is None


def _row(pk, item_type, raw_id, start, end, payload, *, user_id=7, task_id="t-1"):
    return DraftSourceItem(
        timeline_draft_source_item_id=pk,
        task_id=task_id,
        user_id=user_id,
        item_type=item_type,
        raw_id=raw_id,
        start_at=start,
        end_at=end,
        payload=payload,
    )


def test_rows_to_snapshot_assembles_and_derives():
    rows = [
        _row(101, "STAY", "raw-a", datetime(2026, 7, 8, 9, 0), datetime(2026, 7, 8, 10, 0), {"place": "집"}),
        _row(102, "NOTIFICATION", "raw-b", datetime(2026, 7, 8, 8, 0), None, {"appName": "카카오톡"}),
    ]

    snap = _rows_to_snapshot("t-1", rows)

    assert snap.task_id == "t-1"
    # record_date 는 가장 이른 start_at 의 날짜에서 파생한다.
    assert snap.record_date == "2026-07-08"
    assert snap.record_time_zone == "Asia/Seoul"
    assert snap.user_memory is None
    assert {item.id for item in snap.source_items} == {101, 102}
    stay = next(item for item in snap.source_items if item.id == 101)
    assert stay.raw_id == "raw-a"
    assert stay.item_type.value == "STAY"
    assert stay.start_at == "2026-07-08T09:00:00"
    # window: start=min(start_at)=08:00, end=max(end_at,start_at)=10:00
    assert snap.timeline_window.start_time == "2026-07-08T08:00:00"
    assert snap.timeline_window.end_time == "2026-07-08T10:00:00"


def test_rows_to_snapshot_rejects_missing_start_at():
    rows = [
        _row(
            101,
            "HEALTH",
            "raw-a",
            None,
            None,
            {"metric": "STEPS", "value": 100},
        )
    ]

    with pytest.raises(SourceBatchError, match="start_at"):
        _rows_to_snapshot("t-1", rows)


def test_rows_to_snapshot_rejects_mixed_users():
    rows = [
        _row(
            101,
            "PHOTO",
            "raw-a",
            datetime(2026, 7, 8, 9, 0),
            None,
            {},
            user_id=7,
        ),
        _row(
            102,
            "PHOTO",
            "raw-b",
            datetime(2026, 7, 8, 10, 0),
            None,
            {},
            user_id=8,
        ),
    ]

    with pytest.raises(SourceBatchError, match="여러 user_id"):
        _rows_to_snapshot("t-1", rows)


def test_rows_to_snapshot_rejects_duplicate_raw_ids():
    rows = [
        _row(101, "PHOTO", "raw-a", datetime(2026, 7, 8, 9, 0), None, {}),
        _row(102, "PHOTO", "raw-a", datetime(2026, 7, 8, 10, 0), None, {}),
    ]

    with pytest.raises(SourceBatchError, match="중복 raw_id"):
        _rows_to_snapshot("t-1", rows)


def test_load_snapshot_from_file():
    snapshot = load_snapshot_from_file(_SAMPLE)
    assert snapshot.task_id == "2026-07-08"
    assert len(snapshot.source_items) == 35
    assert snapshot.source_items[0].id == 1
    assert snapshot.source_items[0].raw_id == "02ecc2e5-bd40-45ae-b179-896a77bb3d16"
    assert snapshot.timeline_window.start_time == "2026-07-08T00:00"
