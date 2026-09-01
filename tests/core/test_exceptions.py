"""예외 → 코드 매핑과, 실패를 구조화 로그로 남기는 공통 헬퍼를 검증한다."""

import asyncio
import json
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
from app.core.execution_context import ExecutionStage, execution_context
from app.core.logging import JsonLogFormatter
from app.core.structured import StructuredOutputError
from app.services.draft_edit import DraftEditError
from app.services.source_contract import SourceBatchError
from app.services.timeline_validator import (
    TimelineValidationError,
    TimelineViolation,
    TimelineViolationCode,
)

logger = logging.getLogger("tests.core.test_exceptions")


def _diagnostic(caplog) -> logging.LogRecord:
    """`report_error` 가 남긴 **진단 줄**을 고른다(이슈 #101).

    같은 호출이 표식 달린 `app.degraded` 운영 이벤트도 함께 내므로, `records[-1]` 은
    이제 그쪽을 집는다. 이 테스트들이 보려는 것은 예외 원문과 자유 필드가 남는
    진단 줄이라 로거 이름으로 갈라낸다.
    """

    records = [record for record in caplog.records if record.name != "app.operational"]
    assert records, "진단 줄이 없습니다."
    return records[-1]



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
    exc = AppError("내부 경로 /srv/secret 접근 실패", code=ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED)

    assert exc.detail == "내부 경로 /srv/secret 접근 실패"
    assert exc.message == message_for(ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED)
    assert "/srv/secret" not in exc.message


def test_report_error_puts_code_in_a_structured_field(caplog):
    """errorCode 로 필터·집계하려면 값이 메시지 문자열이 아니라 필드에 있어야 한다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        returned = report_error(
            logger,
            ErrorCode.PIPELINE_TIMEOUT,
            "타임라인 처리 timeout",
            exc=asyncio.TimeoutError(),
            context={"taskId": "task-1"},
        )

    assert returned is ErrorCode.PIPELINE_TIMEOUT
    record = _diagnostic(caplog)
    assert record.getMessage() == "타임라인 처리 timeout"
    assert record.fields["errorCode"] == int(ErrorCode.PIPELINE_TIMEOUT)
    assert record.fields["errorType"] == "TimeoutError"
    assert record.fields["taskId"] == "task-1"


def test_report_error_keeps_original_message_in_local_logs(caplog):
    """원본 예외 메시지는 진단에 필요하므로 **로컬** 로그에는 남아야 한다.

    이 줄은 Elasticsearch 로 가지 않는다(이슈 #53). 수집 대상은 운영 이벤트뿐이고,
    거기에는 코드와 안전한 enum 만 실린다.
    """

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(
            logger,
            ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED,
            "저장 실패",
            exc=RuntimeError("connection to 10.0.0.5:3306 refused"),
        )

    assert (
        _diagnostic(caplog).fields["errorMessage"]
        == "connection to 10.0.0.5:3306 refused"
    )


def test_report_error_output_is_not_collected(caplog):
    """report_error 는 코드 부여와 로컬 진단이다. 수집 표식을 만들지 않는다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(
            logger,
            ErrorCode.INTERNAL_ERROR,
            "미처리 예외",
            exc=RuntimeError("boom"),
        )

    record = _diagnostic(caplog)
    assert not hasattr(record, "operational_event")
    payload = json.loads(JsonLogFormatter().format(record))
    assert "event.dataset" not in payload


def test_report_error_records_stage_and_agent_fields(caplog):
    """어느 단계에서 깨졌는지가 로그 필드로 남아야 추적이 된다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(
            logger,
            ErrorCode.EVENT_AGENT_FAILED,
            "Event Agent 실행 실패",
            exc=RuntimeError("boom"),
            stage=ExecutionStage.EVENT_AGENT,
            agent="calendar",
            context={"fallback": "empty_result"},
        )

    fields = _diagnostic(caplog).fields
    assert fields["errorCode"] == int(ErrorCode.EVENT_AGENT_FAILED)
    assert fields["stage"] == "EVENT_AGENT"
    assert fields["agent"] == "calendar"
    assert fields["fallback"] == "empty_result"


def test_report_error_omits_empty_optional_fields(caplog):
    """값 없는 항목까지 null 로 채우면 매핑만 늘고 검색에는 도움이 안 된다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(logger, ErrorCode.LLM_CALL_FAILED, "LLM 호출 실패")

    fields = _diagnostic(caplog).fields
    assert fields == {"errorCode": int(ErrorCode.LLM_CALL_FAILED)}


def test_report_error_with_traceback_leaves_message_to_the_formatter(caplog):
    """exc_info 를 남길 때는 포매터가 error.message 를 채우므로 중복하지 않는다."""

    with caplog.at_level(logging.ERROR, logger=logger.name):
        report_error(
            logger,
            ErrorCode.INTERNAL_ERROR,
            "미처리 예외",
            exc=RuntimeError("boom"),
            level=logging.ERROR,
            exc_info=True,
        )

    record = _diagnostic(caplog)
    assert record.exc_info is not None
    assert record.fields["errorType"] == "RuntimeError"
    assert "errorMessage" not in record.fields


def test_report_error_without_execution_context_does_not_raise(caplog):
    """스크립트·단위 테스트처럼 실행 컨텍스트가 없어도 로그는 남아야 한다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report_error(logger, ErrorCode.LLM_CALL_FAILED, "LLM 호출 실패")

    assert _diagnostic(caplog).fields["errorCode"] == 1203


def test_report_error_inside_execution_context_keeps_task_correlation(caplog):
    """taskId 는 실행 컨텍스트가 붙이므로 호출부가 매번 넘기지 않아도 된다."""

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with execution_context("task-ctx"):
            report_error(logger, ErrorCode.LLM_CALL_FAILED, "LLM 호출 실패")

    # 필드에는 없고, 포매터가 컨텍스트에서 읽어 넣는다.
    assert "taskId" not in _diagnostic(caplog).fields


# --- report_error 가 함께 내는 저하 이벤트 (이슈 #101) -------------------------


def _operational(caplog) -> list[dict]:
    return [
        payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
    ]


def test_report_error_also_emits_a_collected_degraded_event(caplog):
    """진단 줄은 표식이 없다. 같은 실패를 표식 달린 이벤트로 한 건 더 낸다."""

    with caplog.at_level(logging.DEBUG):
        report_error(
            logger,
            ErrorCode.QUESTION_GENERATION_FAILED,
            "Question Agent 실행 실패",
            exc=RuntimeError("boom"),
            stage=ExecutionStage.QUESTION_AGENT,
        )

    # 진단 줄에는 여전히 표식이 없다.
    payload = json.loads(JsonLogFormatter().format(_diagnostic(caplog)))
    assert "event.dataset" not in payload

    events = _operational(caplog)
    assert len(events) == 1
    assert events[0]["event.action"] == "app.degraded"
    assert events[0]["event.outcome"] == "failure"
    assert events[0]["component"] == ExecutionStage.QUESTION_AGENT.value
    assert events[0]["errorCode"] == int(ErrorCode.QUESTION_GENERATION_FAILED)
    assert events[0]["errorType"] == "RuntimeError"


def test_degraded_event_never_carries_the_exception_text_or_free_fields(caplog):
    """**이 테스트가 이 변경의 안전성 전부다.**

    `report_error` 는 예외 원문(`errorMessage`)과 호출부가 준 임의 `context` 를 진단
    줄에 남긴다. 그것들이 표식 달린 줄로 새면 Elasticsearch 로 그대로 나간다.
    emitter 의 allowlist 가 막는다는 것을 값으로 고정한다.
    """

    with caplog.at_level(logging.DEBUG):
        report_error(
            logger,
            ErrorCode.SOURCE_ITEM_NORMALIZE_FAILED,
            "수집 항목 정규화 실패",
            exc=ValueError("장소는 강남역 스타벅스입니다"),
            context={
                "rawId": "raw-사용자-식별자",
                "placeName": "강남역 스타벅스",
                "taskToken": "tok-secret",
            },
            stage=ExecutionStage.REQUEST,
            exc_info=True,
        )

    events = _operational(caplog)
    assert len(events) == 1
    serialized = json.dumps(events[0], ensure_ascii=False)
    for leaked in (
        "강남역 스타벅스",
        "raw-사용자-식별자",
        "tok-secret",
        "errorMessage",
        "placeName",
        "rawId",
        "Traceback",
        "error.stack_trace",
    ):
        assert leaked not in serialized, f"{leaked} 가 수집 대상 줄로 샜습니다."

    # 진단 줄에는 그대로 남아 있어야 한다. 원문을 잃으려는 변경이 아니다.
    assert _diagnostic(caplog).fields["errorType"] == "ValueError"


def test_emit_false_keeps_the_diagnostic_line_and_drops_the_event(caplog):
    """항목 단위 루프가 이벤트 수십 건을 내지 않게 하는 유일한 장치다."""

    with caplog.at_level(logging.DEBUG):
        report_error(
            logger,
            ErrorCode.STRUCTURED_OUTPUT_INVALID,
            "항목 검증 실패로 제외",
            exc=ValueError("x"),
            emit=False,
        )

    assert _operational(caplog) == []
    assert _diagnostic(caplog).fields["errorCode"] == int(
        ErrorCode.STRUCTURED_OUTPUT_INVALID
    )


def test_report_error_forwards_llm_diagnostics_to_the_event(caplog):
    """LLM 실패는 최대한 모은다 — provider·model·stopReason 이 ES 로 나가야 한다."""

    with caplog.at_level(logging.DEBUG):
        report_error(
            logger,
            ErrorCode.STRUCTURED_OUTPUT_INVALID,
            "Bedrock 구조화 응답 없음",
            exc=StructuredOutputError("empty"),
            stage=ExecutionStage.LLM,
            provider="bedrock",
            model="global.amazon.nova-2-lite-v1:0",
            provider_version="1.40.0",
            duration_ms=812.5,
            context={
                "stopReason": "max_tokens",
                "contentBlockKinds": [],
                "tokenUsage": {"inputTokens": 4096},
            },
        )

    event = _operational(caplog)[0]
    assert event["component"] == ExecutionStage.LLM.value
    assert event["provider"] == "bedrock"
    assert event["model"] == "global.amazon.nova-2-lite-v1:0"
    assert event["providerVersion"] == "1.40.0"
    assert event["durationMs"] == 812.5
    assert event["stopReason"] == "max_tokens"
    assert event["tokenUsage"] == {"inputTokens": 4096}


def test_degraded_event_uses_the_task_id_of_the_running_context(caplog):
    """완료 이벤트와 같은 taskId 로 묶여야 '성공했지만 무엇을 잃었는지' 가 보인다."""

    with caplog.at_level(logging.DEBUG):
        with execution_context("task-101"):
            report_error(
                logger,
                ErrorCode.EVENT_AGENT_FAILED,
                "Event Agent 실행 실패",
                exc=RuntimeError("boom"),
                stage=ExecutionStage.EVENT_AGENT,
                agent="calendar",
            )

    event = _operational(caplog)[0]
    assert event["taskId"] == "task-101"
    # 진단 줄은 `agent`, 수집되는 이벤트는 `agentName` 이다(#109).
    assert event["agentName"] == "calendar"
