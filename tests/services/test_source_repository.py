"""수집 스냅샷 저장소(InMemorySourceRepository) 검증."""

from pathlib import Path

from app.services.source_repository import (
    InMemorySourceRepository,
    load_snapshot_from_file,
)
from tests.fixtures.requests import default_source_items, make_snapshot

_SAMPLE = Path(__file__).resolve().parents[2] / "data" / "input" / "2026-07-08.json"


def test_put_then_get_roundtrip():
    repo = InMemorySourceRepository()
    snapshot = make_snapshot(task_id="task-abc", source_items=default_source_items())
    repo.put(snapshot)

    got = repo.get("task-abc")
    assert got is snapshot
    assert len(got.source_items) == 7


def test_get_missing_returns_none():
    assert InMemorySourceRepository().get("missing") is None


def test_load_snapshot_from_file():
    snapshot = load_snapshot_from_file(_SAMPLE)
    assert snapshot.task_id == "2026-07-08"
    assert len(snapshot.source_items) == 35
    assert snapshot.source_items[0].id == 1
    assert snapshot.source_items[0].raw_id == "02ecc2e5-bd40-45ae-b179-896a77bb3d16"
    assert snapshot.timeline_window.start_time == "2026-07-08T00:00"
