"""flush_task_observations 의 로컬 저장·no-op·격리 검증."""

import logging

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


async def test_flush_noop_when_all_disabled(monkeypatch, tmp_path, caplog) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", None)
    monkeypatch.setattr(settings, "obs_enabled", False)
    monkeypatch.setattr(settings, "es_url", "")

    with caplog.at_level(logging.INFO, logger="app.core.observability.runtime"):
        await flush_task_observations(None, task_id="t1")

    assert (
        "관측 수집 건너뜀: taskId=t1, obsEnabled=False, "
        "esUrlConfigured=False, localOutputConfigured=False"
    ) in caplog.text


async def test_flush_logs_es_skip_reason(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", "local-observations")
    monkeypatch.setattr(settings, "obs_enabled", False)
    monkeypatch.setattr(settings, "es_url", "http://unused")
    monkeypatch.setattr(
        "app.core.observability.runtime._write_local",
        lambda *_args, **_kwargs: None,
    )

    with caplog.at_level(logging.INFO, logger="app.core.observability.runtime"):
        await flush_task_observations(_buffer(), task_id="t1")

    assert (
        "관측 ES 전송 건너뜀: taskId=t1, obsEnabled=False, "
        "esUrlConfigured=True, documents=2"
    ) in caplog.text


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


def test_build_observer_applies_configured_none_capture(monkeypatch) -> None:
    monkeypatch.setattr(settings, "obs_local_dir", "local-observations")
    monkeypatch.setattr(settings, "obs_enabled", False)
    monkeypatch.setattr(settings, "obs_content_capture", "NONE")

    observer, buffer = build_task_observer()
    assert observer is not None
    assert buffer is not None

    observer.emit(
        ObservationEvent(
            task_id="t1",
            stage=ObservationStage.LLM,
            event_type=ObservationEventType.PROMPT,
            payload={"prompt": "본문"},
        )
    )

    assert buffer.events[0].payload["prompt"]["contentCaptured"] is False
