"""예외 → 코드 매핑과, 로그·관측을 한 번에 남기는 공통 헬퍼를 검증한다."""

import asyncio
import logging

import pytest

from app.agents.repair.tools import RepairToolError
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import (
    AppError,
    code_of,
    code_of_or,
    report_error,
    safe_message,
)
from app.core.observability import (
    InMemoryObservationSink,
    ObservationEventType,
    ObservationStage,
    Observer,
    observation_context,
)
from app.core.observability.sinks import CompositeObservationError
from app.core.structured import StructuredOutputError
from app.services.draft_edit import DraftEditError
from app.services.source_repository import SourceBatchError
from app.services.timeline_validator import (
    TimelineValidationError,
    TimelineViolation,
    TimelineViolationCode,
)

logger = logging.getLogger("tests.core.test_exceptions")


def _validation_error() -> TimelineValidationError:
    return TimelineValidationError(
        [TimelineViolation(TimelineViolationCode.TITLE_EMPTY, "이벤트 e-1: title 비었음")]
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (StructuredOutputError("x"), ErrorCode.STRUCTURED_OUTPUT_INVALID),
        (SourceBatchError("x"), ErrorCode.SOURCE_CONTRACT_VIOLATION),
        (_validation_error(), ErrorCode.TIMELINE_STORAGE_VALIDATION_FAILED),
        (DraftEditError("x"), ErrorCode.DRAFT_EDIT_FAILED),
        (RepairToolError("x"), ErrorCode.REPAIR_TOOL_FAILED),
        (CompositeObservationError([]), ErrorCode.OBSERVATION_EMIT_FAILED),
        (asyncio.TimeoutError(), ErrorCode.PIPELINE_TIMEOUT),
        (TimeoutError(), ErrorCode.PIPELINE_TIMEOUT),
        (RuntimeError("분류되지 않음"), ErrorCode.INTERNAL_ERROR),
        (KeyError("분류되지 않음"), ErrorCode.INTERNAL_ERROR),
    ],
)
def test_exception_maps_to_expected_code(exc: BaseException, expected: ErrorCode):
    assert code_of(exc) is expected


def test_legacy_base_classes_still_catchable():
    """기존에 이 예외들을 표준 타입으로 잡던 호출부가 그대로 동작해야 한다."""

    assert isinstance(SourceBatchError("x"), ValueError)
    assert isinstance(CompositeObservationError([]), RuntimeError)


def test_safe_message_never_returns_the_original_text():
    """원본 예외 메시지에는 내부 식별자가 들어 있어 그대로 내보내면 안 된다."""

    exc = SourceBatchError("taskId=task-1 의 rawId=abc-123 가 중복입니다")

    message = safe_message(exc)

    assert message == message_for(ErrorCode.SOURCE_CONTRACT_VIOLATION)
    assert "task-1" not in message
    assert "abc-123" not in message


def test_code_of_or_preserves_specific_code_and_uses_stage_fallback() -> None:
    assert (
        code_of_or(
            StructuredOutputError("invalid"),
            ErrorCode.EVENT_AGENT_FAILED,
        )
        is ErrorCode.STRUCTURED_OUTPUT_INVALID
    )
    assert (
        code_of_or(RuntimeError("unknown"), ErrorCode.EVENT_AGENT_FAILED)
        is ErrorCode.EVENT_AGENT_FAILED
    )


def test_app_error_message_is_catalog_message_not_detail():
    exc = AppError("내부 경로 /srv/secret 접근 실패", code=ErrorCode.DATABASE_ERROR)

    assert exc.detail == "내부 경로 /srv/secret 접근 실패"
    assert exc.message == message_for(ErrorCode.DATABASE_ERROR)
    assert "/srv/secret" not in exc.message


def test_report_error_logs_error_code_first(caplog):
    """로그를 errorCode 로 필터·집계하려면 필드 이름과 위치가 흔들리면 안 된다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        returned = report_error(
            logger,
            ErrorCode.PIPELINE_TIMEOUT,
            "타임라인 처리 timeout",
            exc=asyncio.TimeoutError(),
            context={"taskId": "task-1"},
            emit=False,
        )

    assert returned is ErrorCode.PIPELINE_TIMEOUT
    assert (
        "타임라인 처리 timeout: errorCode=1201, errorType=TimeoutError, taskId=task-1"
        in caplog.text
    )


def test_report_error_keeps_original_message_in_logs(caplog):
    """원본 예외 메시지는 진단에 필요하므로 로그에는 남아야 한다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(
            logger,
            ErrorCode.DATABASE_ERROR,
            "저장 실패",
            exc=RuntimeError("connection to 10.0.0.5:3306 refused"),
            emit=False,
        )

    assert "connection to 10.0.0.5:3306 refused" in caplog.text


def test_report_error_emits_observation_with_same_code():
    """로그와 관측이 같은 코드를 써야 한 실패를 두 곳에서 이어 볼 수 있다."""

    sink = InMemoryObservationSink()
    observer = Observer(sink)

    with observation_context("task-1", observer):
        report_error(
            logger,
            ErrorCode.EVENT_AGENT_FAILED,
            "Event Agent 실행 실패",
            exc=RuntimeError("boom"),
            stage=ObservationStage.EVENT_AGENT,
            agent="calendar",
            payload={"fallback": "empty_result"},
        )

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_type is ObservationEventType.FAILED
    assert event.stage is ObservationStage.EVENT_AGENT
    assert event.agent == "calendar"
    assert event.payload == {
        "errorCode": int(ErrorCode.EVENT_AGENT_FAILED),
        "errorType": "RuntimeError",
        "fallback": "empty_result",
    }


def test_report_error_payload_cannot_override_common_error_fields():
    sink = InMemoryObservationSink()
    observer = Observer(sink)

    with observation_context("task-1", observer):
        report_error(
            logger,
            ErrorCode.DATABASE_ERROR,
            "저장 실패",
            exc=RuntimeError("boom"),
            payload={"errorCode": 9999, "errorType": "FakeError"},
        )

    assert sink.events[0].payload["errorCode"] == int(ErrorCode.DATABASE_ERROR)
    assert sink.events[0].payload["errorType"] == "RuntimeError"


def test_report_error_with_emit_false_writes_no_observation():
    """관측 모듈 자신의 실패는 관측으로 알릴 수 없다(재귀한다)."""

    sink = InMemoryObservationSink()
    observer = Observer(sink)

    with observation_context("task-1", observer):
        report_error(
            logger,
            ErrorCode.OBSERVATION_EMIT_FAILED,
            "관측 이벤트 기록 실패",
            exc=RuntimeError("sink down"),
            emit=False,
        )

    assert sink.events == []


def test_report_error_without_observation_context_does_not_raise(caplog):
    """스크립트·단위 테스트처럼 관측 컨텍스트가 없어도 로그는 남아야 한다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(logger, ErrorCode.LLM_CALL_FAILED, "LLM 호출 실패")

    assert "errorCode=1203" in caplog.text
