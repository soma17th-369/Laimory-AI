"""data/input 기반 Event Agent live 테스트 공통 헬퍼."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from app.agents.events.photo import PhotoEventAgent
from app.agents.events.photo.describer import VisionPhotoDescriber
from app.agents.events.photo.image_source import LocalFilePhotoImageSource
from app.schemas import TimelineDraftRequest
from app.core.observability import (
    CompositeObservationSink,
    ContentCapture,
    InMemoryObservationSink,
    JsonLinesObservationSink,
    ObservationEventType,
    ObservationStage,
    Observer,
    emit_observation,
    observation_context,
    observation_scope,
)
from tests.fixtures.live_data import resolve_live_data_case
from tests.fixtures.live_llm import prepare_live_llm_env
from tests.fixtures.live_output import current_live_run

def vision_photo_event_agent() -> PhotoEventAgent:
    """data/input 의 실제 이미지를 vision 으로 보는 Photo Event Agent.

    입력 JSON 의 `photoFile`(→`clientPhotoUri`)이 실제 파일명과 일치하므로,
    `LocalFilePhotoImageSource(INPUT_DIR)` 가 실제 JPEG 바이너리를 읽어 vision
    호출에 실어 보낸다. 이미지를 못 구한 사진은 메타데이터 fallback 으로 채운다.
    """

    return PhotoEventAgent(
        describer=VisionPhotoDescriber(
            image_source=LocalFilePhotoImageSource(
                resolve_live_data_case().image_dir
            )
        )
    )


def write_agent_result(agent_name: str, payload: dict[str, Any]) -> Path:
    data_case = resolve_live_data_case()
    return current_live_run(data_case.date).write_json(
        Path("event-agents") / f"{agent_name}.json",
        payload,
    )


class LiveObserver(Observer):
    """JSONL을 저장하면서 실제 provider 응답 성공 여부도 검증한다."""

    def __init__(self, output_path: Path | TextIO) -> None:
        self.memory_sink = InMemoryObservationSink()
        super().__init__(
            CompositeObservationSink(
                [
                    JsonLinesObservationSink(output_path),
                    self.memory_sink,
                ]
            ),
            content_capture=ContentCapture.SANITIZED,
        )

    def assert_llm_calls_succeeded(self) -> None:
        llm_events = [
            event
            for event in self.memory_sink.events
            if event.stage is ObservationStage.LLM
        ]
        responses = [
            event
            for event in llm_events
            if event.event_type is ObservationEventType.RESPONSE
        ]
        failures = [
            event
            for event in llm_events
            if event.event_type is ObservationEventType.FAILED
        ]
        failure_types = sorted(
            {
                str(event.payload.get("errorType", "unknown"))
                for event in failures
            }
        )
        assert responses, "실제 LLM RESPONSE 관측 이벤트가 없습니다."
        assert not failures, (
            "실제 LLM 호출 실패가 관측되었습니다: "
            f"count={len(failures)}, errorTypes={failure_types}"
        )


def live_observer(data_date: str) -> LiveObserver:
    run = current_live_run(data_date)
    return LiveObserver(run.path_for("observations.jsonl"))


def run_live_event_agent(agent, request: TimelineDraftRequest):
    """개별 Event Agent를 transaction 관측 컨텍스트 안에서 실행한다."""

    observer = live_observer(request.date)
    name = getattr(agent, "name", type(agent).__name__)
    with observation_context(request.transaction_id, observer):
        with observation_scope(ObservationStage.EVENT_AGENT, agent=name):
            emit_observation(
                ObservationEventType.STARTED,
                payload={
                    "request": request.model_dump(by_alias=True, mode="json")
                },
            )
            result = agent.generate(request)
            emit_observation(
                ObservationEventType.COMPLETED,
                payload={"result": result.model_dump(by_alias=True, mode="json")},
            )
    observer.assert_llm_calls_succeeded()
    return result


def dump_agent_result(agent_name: str, result) -> dict[str, Any]:
    actual = result.model_dump(by_alias=True, mode="json")
    output_path = write_agent_result(agent_name, actual)
    print(
        f"\n[{agent_name}] output={output_path} "
        f"candidates={len(result.candidates)} "
        f"fragments={len(result.fragments)} "
        f"warnings={len(result.warnings)}"
    )
    print(json.dumps(actual, ensure_ascii=False, indent=2))
    return actual


def assert_agent_result_shape(actual: dict[str, Any], result) -> None:
    assert isinstance(actual["candidates"], list)
    assert isinstance(actual["fragments"], list)
    assert isinstance(actual["warnings"], list)
    assert result.candidates or result.fragments or result.warnings


def full_live_request() -> TimelineDraftRequest:
    """실제 입력 JSON 전체를 정규화한 요청을 만든다."""

    return resolve_live_data_case().load_request()


def _domain_request(
    request: TimelineDraftRequest, **domains: list
) -> TimelineDraftRequest:
    return request.model_copy(
        update={
            "stays": domains.get("stays", []),
            "movements": domains.get("movements", []),
            "calendars": domains.get("calendars", []),
            "healths": domains.get("healths", []),
            "notifications": domains.get("notifications", []),
            "photos": domains.get("photos", []),
        }
    )


def stay_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(
        request,
        stays=request.stays,
        movements=request.movements,
    )


def calendar_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, calendars=request.calendars)


def photo_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, photos=request.photos)


def notification_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, notifications=request.notifications)


def health_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, healths=request.healths)
