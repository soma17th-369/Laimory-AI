"""Elasticsearch 수집 경계 검증 (이슈 #53).

지키는 것은 네 가지다.

1. 수집 표식은 emitter 만 만들 수 있다(일반 로그는 위조하지 못한다).
2. 이벤트마다 허용된 필드만 나간다. 임의 필드·본문·예외 원문은 버려진다.
3. 운영 이벤트에는 예외 traceback 이 붙지 않는다.
4. 로깅 실패가 호출부를 깨뜨리지 않는다.
"""

import json
import logging

import pytest

from app.core.execution_context import (
    ExecutionStage,
    execution_context,
    execution_scope,
)
from app.core.logging import JsonLogFormatter, get_logger, log_fields
from app.core.operational_logging import (
    ACTION_FIELD,
    DATASET_FIELD,
    EVENT_DATASET,
    OUTCOME_FIELD,
    EventOutcome,
    OperationalEvent,
    emit_event,
    http_level,
    http_outcome,
)

OPERATIONAL_LOGGER = "app.operational"


def _events(caplog) -> list[dict]:
    """운영 이벤트 줄을 실제 JSON 으로 직렬화해 돌려준다."""

    formatter = JsonLogFormatter()
    lines = [
        formatter.format(record)
        for record in caplog.records
        if record.name == OPERATIONAL_LOGGER
    ]
    for line in lines:
        assert "\n" not in line, "한 줄에 개행이 있으면 Filebeat 가 이벤트를 쪼갠다."
    return [json.loads(line) for line in lines]


@pytest.fixture
def capture(caplog):
    with caplog.at_level(logging.DEBUG):
        yield caplog


def test_event_carries_the_collection_marker_and_common_fields(capture) -> None:
    emit_event(
        OperationalEvent.HTTP_REQUEST_COMPLETED,
        method="GET",
        route="/v1/timeline",
        httpStatus=200,
        durationMs=12.5,
    )

    payload = _events(capture)[-1]
    assert payload[DATASET_FIELD] == EVENT_DATASET
    assert payload[ACTION_FIELD] == OperationalEvent.HTTP_REQUEST_COMPLETED.value
    assert payload[OUTCOME_FIELD] == EventOutcome.SUCCESS.value
    for field in ("timestamp", "log.level", "logger", "message", "service", "environment", "version"):
        assert field in payload, f"필수 필드 누락: {field}"
    assert payload["httpStatus"] == 200
    assert payload["route"] == "/v1/timeline"


def test_fields_outside_the_event_allowlist_never_reach_the_line(capture) -> None:
    """호출부 실수 하나로 사용자 콘텐츠가 수집되면 안 된다."""

    emit_event(
        OperationalEvent.TIMELINE_TASK_COMPLETED,
        taskId="task-1",
        status="SUCCESS",
        durationMs=10.0,
        # 아래는 전부 허용 목록에 없다.
        title="점심 식사",
        prompt="사용자 원문",
        photoUrl="https://example.com/a.jpg?X-Amz-Signature=abc",
        rawId="raw-77",
    )

    payload = _events(capture)[-1]
    assert payload["taskId"] == "task-1"
    assert payload["status"] == "SUCCESS"
    serialized = json.dumps(payload, ensure_ascii=False)
    for leaked in ("점심 식사", "사용자 원문", "example.com", "raw-77"):
        assert leaked not in serialized


def test_none_values_are_dropped(capture) -> None:
    emit_event(
        OperationalEvent.TIMELINE_TASK_COMPLETED,
        taskId="task-1",
        status="SUCCESS",
        durationMs=1.0,
        errorCode=None,
        failureStage=None,
    )

    payload = _events(capture)[-1]
    assert "errorCode" not in payload
    assert "failureStage" not in payload


def test_execution_context_does_not_leak_extra_fields_into_the_event(capture) -> None:
    """운영 이벤트의 필드는 emitter 가 조립한 것뿐이다."""

    with execution_context("task-ctx"):
        with execution_scope(ExecutionStage.EVENT_AGENT, agent="photo", iteration=2):
            emit_event(
                OperationalEvent.TIMELINE_TASK_COMPLETED,
                status="FAILED",
                outcome=EventOutcome.FAILURE,
                errorCode=1201,
            )

    payload = _events(capture)[-1]
    # taskId 는 이 이벤트가 허용하므로 컨텍스트에서 채운다.
    assert payload["taskId"] == "task-ctx"
    # 그 밖의 컨텍스트 값은 허용 목록에 없으므로 붙지 않는다.
    assert "agent" not in payload
    assert "iteration" not in payload
    assert "stage" not in payload
    assert payload["errorCode"] == 1201
    assert payload[OUTCOME_FIELD] == EventOutcome.FAILURE.value


def test_ordinary_logs_cannot_forge_the_marker(capture) -> None:
    """표식을 흉내 낸 일반 로그가 수집 대상이 되면 경계가 무너진다."""

    logger = get_logger("tests.core.forged")
    logger.info(
        "표식 위조 시도",
        extra=log_fields(
            **{
                "event.dataset": EVENT_DATASET,
                "event.action": "http.request.completed",
                "title": "사용자 원문",
            }
        ),
    )

    record = next(
        item for item in capture.records if item.name == "tests.core.forged"
    )
    payload = json.loads(JsonLogFormatter().format(record))
    assert DATASET_FIELD not in payload
    assert ACTION_FIELD not in payload


def test_operational_event_has_no_traceback_even_with_exc_info(capture) -> None:
    """예외 원문·traceback 은 운영 이벤트에 실리지 않는다."""

    logger = logging.getLogger(OPERATIONAL_LOGGER)
    try:
        raise RuntimeError("connection to 10.0.0.5:3306 refused")
    except RuntimeError:
        import sys

        # emitter 를 거치지 않고 exc_info 를 억지로 붙여도 포매터가 막는다.
        record = logger.makeRecord(
            OPERATIONAL_LOGGER,
            logging.ERROR,
            __file__,
            1,
            "%s",
            ("외부 연동 호출 완료",),
            sys.exc_info(),
            extra={
                "operational_event": {
                    DATASET_FIELD: EVENT_DATASET,
                    ACTION_FIELD: OperationalEvent.DEPENDENCY_REQUEST_COMPLETED.value,
                    OUTCOME_FIELD: EventOutcome.FAILURE.value,
                    "errorCode": 1105,
                }
            },
        )

    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["errorCode"] == 1105
    assert "error.stack_trace" not in payload
    assert "error.message" not in payload
    assert "10.0.0.5" not in json.dumps(payload, ensure_ascii=False)


def test_emit_never_raises_when_logging_fails(monkeypatch) -> None:
    """관측이 요청 처리나 백그라운드 작업을 깨뜨리면 안 된다."""

    class _Broken:
        def log(self, *args, **kwargs):
            raise RuntimeError("handler down")

    monkeypatch.setattr("app.core.operational_logging._logger", _Broken())

    emit_event(OperationalEvent.SERVER_STARTED, appEnv="test", logFormat="json")


def test_http_level_and_outcome_follow_the_status_code() -> None:
    assert http_level(200) == logging.INFO
    assert http_level(302) == logging.INFO
    assert http_level(422) == logging.WARNING
    assert http_level(500) == logging.ERROR
    assert http_outcome(204) is EventOutcome.SUCCESS
    assert http_outcome(404) is EventOutcome.FAILURE
