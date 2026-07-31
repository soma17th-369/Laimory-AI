"""운영 로그 계약 검증 (이슈 #47).

Filebeat 가 stdout 을 줄 단위로 읽어 Elasticsearch 로 보내므로, 한 줄이 유효한 JSON
하나가 아니면 그 이벤트를 통째로 잃는다. 여기서 지키는 것은 네 가지다.

1. 한 줄 = 유효한 JSON 하나 (예외·개행 포함)
2. 필수 필드가 항상 있다
3. taskId·stage 가 실행 컨텍스트에서 자동으로 붙는다
4. Secret·개인정보·taskToken 이 로그로 새지 않는다
"""

import json
import logging

import pytest

from app.core.execution_context import (
    ExecutionStage,
    execution_context,
    execution_scope,
)
from app.core.logging import (
    SERVICE_NAME,
    JsonLogFormatter,
    log_fields,
    stage_span,
)

REQUIRED_FIELDS = (
    "timestamp",
    "log.level",
    "logger",
    "message",
    "service",
    "environment",
    "version",
)


@pytest.fixture
def formatter() -> JsonLogFormatter:
    return JsonLogFormatter()


def _record(
    message: str = "테스트 로그",
    *,
    level: int = logging.INFO,
    args: tuple = (),
    fields: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tests.logging",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=exc_info,
    )
    if fields is not None:
        record.fields = fields
    return record


def _emitted(formatter: JsonLogFormatter, record: logging.LogRecord) -> dict:
    line = formatter.format(record)
    assert "\n" not in line, "로그 한 줄에 개행이 있으면 Filebeat 가 이벤트를 쪼갠다."
    return json.loads(line)


def test_line_is_one_json_object_with_required_fields(formatter) -> None:
    payload = _emitted(formatter, _record())

    for field in REQUIRED_FIELDS:
        assert field in payload, f"필수 필드 누락: {field}"
    assert payload["log.level"] == "INFO"
    assert payload["logger"] == "tests.logging"
    assert payload["message"] == "테스트 로그"
    assert payload["service"] == SERVICE_NAME


def test_percent_formatting_is_applied_to_the_message(formatter) -> None:
    payload = _emitted(formatter, _record("단계 시작: %s", args=("REQUEST",)))

    assert payload["message"] == "단계 시작: REQUEST"


def test_exception_stays_inside_a_single_line(formatter) -> None:
    """multiline traceback 을 그대로 흘리면 Filebeat 가 줄마다 다른 이벤트로 만든다."""

    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("미처리 예외", level=logging.ERROR, exc_info=sys.exc_info())

    payload = _emitted(formatter, record)

    assert payload["error.type"] == "ValueError"
    assert payload["error.message"] == "boom"
    assert "Traceback" in payload["error.stack_trace"]
    # 줄바꿈은 JSON 문자열 안에 이스케이프돼 있어야 한다.
    assert "\n" in payload["error.stack_trace"]


def test_execution_context_fields_are_injected(formatter) -> None:
    with execution_context("task-log-1"):
        with execution_scope(ExecutionStage.EVENT_AGENT, agent="location"):
            payload = _emitted(formatter, _record())

    assert payload["taskId"] == "task-log-1"
    assert payload["stage"] == "EVENT_AGENT"
    assert payload["agent"] == "location"


def test_caller_fields_win_over_context(formatter) -> None:
    """다른 task 를 대신 기록하는 경로가 있어 호출부 값이 이겨야 한다."""

    with execution_context("task-ambient"):
        payload = _emitted(
            formatter,
            _record(fields={"taskId": "task-explicit", "errorCode": 1201}),
        )

    assert payload["taskId"] == "task-explicit"
    assert payload["errorCode"] == 1201


def test_caller_cannot_overwrite_reserved_fields(formatter) -> None:
    payload = _emitted(
        formatter,
        _record(
            fields={
                "service": "spoofed",
                "message": "spoofed",
                "logger": "x",
                "timestamp": "1999-01-01T00:00:00Z",
            }
        ),
    )

    assert payload["service"] == SERVICE_NAME
    assert payload["message"] == "테스트 로그"
    assert payload["logger"] == "tests.logging"
    assert not payload["timestamp"].startswith("1999")


def test_timestamp_is_not_the_beats_reserved_key(formatter) -> None:
    """`@timestamp` 는 Beats 가 이벤트 시각으로 예약한 이름이라 쓰지 않는다."""

    payload = _emitted(formatter, _record())

    assert "@timestamp" not in payload
    assert payload["timestamp"].endswith("Z")


def test_secrets_and_personal_data_are_masked(formatter) -> None:
    payload = _emitted(
        formatter,
        _record(
            "외부 호출 실패: Authorization: Bearer abc.def.ghi",
            fields={
                "apiKey": "sk-abcdefghijklmnop",
                "taskToken": "task-token-value",
                "contact": "user@example.com / 010-1234-5678",
            },
        ),
    )

    assert "abc.def.ghi" not in payload["message"]
    assert payload["apiKey"] == "[REDACTED]"
    assert payload["taskToken"] == "[REDACTED]"
    assert "user@example.com" not in payload["contact"]
    assert "010-1234-5678" not in payload["contact"]


def test_task_token_never_reaches_the_log_line(formatter) -> None:
    """토큰은 갱신 횟수만 남기고 값은 어떤 자리로도 나가지 않는다."""

    payload = _emitted(
        formatter,
        _record(fields={"taskToken": "secret-token", "tokenRefreshCount": 2}),
    )

    assert "secret-token" not in json.dumps(payload, ensure_ascii=False)
    assert payload["tokenRefreshCount"] == 2


def test_log_fields_drops_none_values() -> None:
    assert log_fields(a=1, b=None, c="x") == {"fields": {"a": 1, "c": "x"}}


def test_stage_span_logs_boundaries_with_duration(caplog) -> None:
    logger = logging.getLogger("tests.logging.stage")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with execution_context("task-span"):
            with stage_span(logger, ExecutionStage.STORAGE, dailyRecordId=7) as outcome:
                outcome["eventCount"] = 3

    start, done = caplog.records[-2], caplog.records[-1]
    assert start.getMessage() == "단계 시작: STORAGE"
    assert start.fields == {"dailyRecordId": 7}
    assert done.getMessage() == "단계 완료: STORAGE"
    assert done.fields["dailyRecordId"] == 7
    assert done.fields["eventCount"] == 3
    assert done.fields["durationMs"] >= 0


def test_stage_span_closes_the_boundary_on_failure(caplog) -> None:
    """실패해도 단계가 어디서 끊겼는지는 남고, 예외는 그대로 올라간다."""

    logger = logging.getLogger("tests.logging.stage")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with pytest.raises(RuntimeError):
            with stage_span(logger, ExecutionStage.MAIN_AGENT):
                raise RuntimeError("boom")

    assert caplog.records[-1].getMessage() == "단계 중단: MAIN_AGENT"
    assert caplog.records[-1].fields["durationMs"] >= 0
