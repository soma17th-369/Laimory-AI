"""flush_task_observations 의 로컬 저장·no-op·격리 검증."""

from app.core.config import settings
from app.core.observability import (
    InMemoryObservationSink,
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
)
from app.core.observability.runtime import build_task_observer, flush_task_observations


def _buffer() -> InMemoryObservationSink:
    sink = InMemoryObservationSink()
    sink.events.append(
        ObservationEvent(
            task_id="t1",
            sequence=0,
            stage=ObservationStage.REQUEST,
            event_type=ObservationEventType.STARTED,
        )
    )
    sink.events.append(
        ObservationEvent(
            task_id="t1",
            sequence=1,
            stage=ObservationStage.FINAL,
            event_type=ObservationEventType.COMPLETED,
        )
    )
    return sink


async def test_flush_writes_only_local_events(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", str(tmp_path))
    monkeypatch.setattr(settings, "obs_enabled", False)

    await flush_task_observations(_buffer(), task_id="t1")

    out = tmp_path / "t1"
    assert (out / "events.jsonl").exists()
    assert not (out / "task.json").exists()
    assert '"taskId": "t1"' in (out / "events.jsonl").read_text(encoding="utf-8")


async def test_flush_noop_when_all_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", None)
    monkeypatch.setattr(settings, "obs_enabled", False)

    # 아무 것도 하지 않고 예외도 없어야 한다.
    await flush_task_observations(_buffer(), task_id="t1")


async def test_flush_empty_buffer_writes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", str(tmp_path))
    monkeypatch.setattr(settings, "obs_enabled", False)

    await flush_task_observations(InMemoryObservationSink(), task_id="t1")

    assert not (tmp_path / "t1").exists()


def test_build_observer_is_true_noop_when_outputs_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", None)
    monkeypatch.setattr(settings, "obs_enabled", False)
    monkeypatch.setattr(settings, "es_url", "http://unused")

    observer, buffer = build_task_observer()

    assert observer is None
    assert buffer is None
