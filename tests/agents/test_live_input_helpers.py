from datetime import datetime, timezone
import io
from pathlib import Path

import pytest

from app.core.observability import (
    ObservationEvent,
    ObservationEventType,
    ObservationStage,
)
from tests.agents.live_input_helpers import (
    LiveObserver,
    calendar_request,
    full_live_request,
    health_request,
    notification_request,
    photo_request,
    stay_request,
)
from tests.fixtures.live_data import resolve_live_data_case
from tests.fixtures.live_llm import resolve_live_llm_config
from tests.fixtures.live_output import build_live_run_context


def test_live_data_case_resolves_date_directory_and_snapshot() -> None:
    case = resolve_live_data_case("2026-07-08")

    assert case.date == "2026-07-08"
    assert case.directory.name == "2026-07-08"
    assert case.snapshot_path == case.directory / "2026-07-08.json"
    assert case.image_dir == case.directory


def test_live_data_case_uses_environment_date(monkeypatch) -> None:
    monkeypatch.setenv("LAIMORY_LIVE_DATA_DATE", "2026-07-08")

    assert resolve_live_data_case().date == "2026-07-08"


def test_live_data_case_rejects_invalid_date() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        resolve_live_data_case("20260708")


def test_live_data_case_reports_missing_date() -> None:
    with pytest.raises(FileNotFoundError, match="입력 디렉터리"):
        resolve_live_data_case("2026-07-09")


def test_live_run_path_contains_date_time_provider_and_model() -> None:
    started_at = datetime(2026, 7, 17, 12, 34, 56, 123456, tzinfo=timezone.utc)

    run = build_live_run_context(
        "2026-07-08",
        provider="OpenAI",
        model="gpt/5:test",
        started_at=started_at,
        output_root=Path("outputs"),
    )

    assert run.directory == (
        Path("outputs")
        / "2026-07-08"
        / "20260717T123456.123456+0000-openai-gpt-5-test"
    )
    assert run.metadata() == {
        "dataDate": "2026-07-08",
        "startedAt": "2026-07-17T12:34:56.123456+00:00",
        "provider": "openai",
        "model": "gpt/5:test",
        "runId": "20260717T123456.123456+0000",
    }


def test_live_run_rejects_path_outside_its_directory() -> None:
    run = build_live_run_context(
        "2026-07-08",
        provider="openai",
        model="gpt-5",
        run_id="run-1",
        output_root=Path("outputs"),
    )

    with pytest.raises(ValueError, match="상대 경로"):
        run.path_for("../outside.json")


def test_live_llm_config_infers_provider_without_exposing_secret() -> None:
    config = resolve_live_llm_config(
        {
            "OPENAI_API_KEY": "secret-value",
            "OPENAI_MODEL": "gpt-5",
            "LAIMORY_LIVE_LLM_SERIAL": "1",
        }
    )

    assert config.provider == "openai"
    assert config.model == "gpt-5"
    assert config.serial is True
    assert "secret-value" not in repr(config)


def test_live_llm_config_reports_missing_model_name() -> None:
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        resolve_live_llm_config(
            {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "secret-value"}
        )


def _llm_event(event_type: ObservationEventType) -> ObservationEvent:
    return ObservationEvent(
        transactionId="tx-live-test",
        stage=ObservationStage.LLM,
        eventType=event_type,
        provider="openai",
        model="test-model",
        payload={},
    )


def test_live_observer_requires_a_real_llm_response() -> None:
    observer = LiveObserver(io.StringIO())
    observer.emit(_llm_event(ObservationEventType.PROMPT))

    with pytest.raises(AssertionError, match="RESPONSE"):
        observer.assert_llm_calls_succeeded()


def test_live_observer_rejects_llm_failure_even_with_response() -> None:
    observer = LiveObserver(io.StringIO())
    observer.emit(_llm_event(ObservationEventType.RESPONSE))
    observer.emit(
        _llm_event(ObservationEventType.FAILED).model_copy(
            update={"payload": {"errorType": "APIConnectionError"}}
        )
    )

    with pytest.raises(AssertionError, match="APIConnectionError"):
        observer.assert_llm_calls_succeeded()


def test_live_input_json_is_normalized_with_raw_ids() -> None:
    request = full_live_request()

    assert request.task_id == "2026-07-08"
    assert request.date == "2026-07-08"
    assert request.timezone == "Asia/Seoul"
    assert request.window.start == "2026-07-08T00:00"
    assert request.window.end == "2026-07-08T23:53:28.969"

    assert len(request.stays) == 6
    assert len(request.movements) == 4
    assert len(request.calendars) == 1
    assert len(request.healths) == 2
    assert len(request.notifications) == 18
    assert len(request.photos) == 4
    notification_raw_ids = {item.raw_id for item in request.notifications}
    photo_raw_ids = {item.raw_id for item in request.photos}
    health_by_metric = {item.metric.value: item for item in request.healths}

    assert "37af39db-f018-4973-bdf1-95724c74f824" in notification_raw_ids
    assert "e015a889-3517-45ac-9e12-ea94702fb7e7" in photo_raw_ids
    assert health_by_metric["SLEEP"].duration_minutes == 340
    assert health_by_metric["STEPS"].value == 10631
    assert request.stays[0].place == "오산운암3단지 주공아파트"
    assert request.movements[0].start.place == "오산운암3단지 주공아파트"


def test_event_agent_live_requests_keep_only_their_own_domains() -> None:
    stay = stay_request()
    assert len(stay.stays) == 6
    assert len(stay.movements) == 4
    assert stay.calendars == []
    assert stay.healths == []
    assert stay.notifications == []
    assert stay.photos == []

    assert len(calendar_request().calendars) == 1
    assert len(photo_request().photos) == 4
    assert len(notification_request().notifications) == 18
    assert len(health_request().healths) == 2
