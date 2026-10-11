"""Elasticsearch 수집 경계 검증 (이슈 #53).

지키는 것은 네 가지다.

1. 수집 표식은 emitter 만 만들 수 있다(일반 로그는 위조하지 못한다).
2. 이벤트마다 허용된 필드만 나간다. 임의 필드·본문·예외 원문은 버려진다.
3. 운영 이벤트에는 예외 traceback 이 붙지 않는다.
4. 로깅 실패가 호출부를 깨뜨리지 않는다.

여기에 사람이 읽는 `message` 계약이 하나 더 붙는다(이슈 #78).

5. 외부 연동 이벤트의 문구는 dependency·operation·outcome 의 고정 매핑으로 정해지고,
   라벨을 모르는 값은 문구에 실리지 않는다.
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
    DegradedComponent,
    EventOutcome,
    OperationalEvent,
    emit_degraded,
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


# --- 사람이 읽는 message (이슈 #78) -------------------------------------


@pytest.mark.parametrize(
    ("operation", "outcome", "expected"),
    [
        ("input", EventOutcome.SUCCESS, "App Server 타임라인 입력 조회 성공"),
        ("input", EventOutcome.FAILURE, "App Server 타임라인 입력 조회 실패"),
        ("result", EventOutcome.SUCCESS, "App Server 타임라인 결과 저장 성공"),
        ("result", EventOutcome.FAILURE, "App Server 타임라인 결과 저장 실패"),
        ("callback", EventOutcome.SUCCESS, "App Server 타임라인 완료 콜백 전송 성공"),
        ("callback", EventOutcome.FAILURE, "App Server 타임라인 완료 콜백 전송 실패"),
        (
            "user-memory-result",
            EventOutcome.SUCCESS,
            "App Server User Memory 결과 저장 성공",
        ),
        (
            "user-memory-result",
            EventOutcome.FAILURE,
            "App Server User Memory 결과 저장 실패",
        ),
    ],
)
def test_completed_message_names_the_operation_and_the_outcome(
    capture, operation, outcome, expected
) -> None:
    """`operation` 을 펼치지 않아도 어떤 호출이 어떻게 끝났는지 보여야 한다."""

    emit_event(
        OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
        outcome=outcome,
        dependency="app-server",
        operation=operation,
        httpStatus=200,
        attempts=1,
        durationMs=12.0,
    )

    assert _events(capture)[-1]["message"] == expected


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("input", "App Server 타임라인 입력 조회 재시도"),
        ("result", "App Server 타임라인 결과 저장 재시도"),
        ("callback", "App Server 타임라인 완료 콜백 전송 재시도"),
        ("user-memory-result", "App Server User Memory 결과 저장 재시도"),
    ],
)
def test_retry_message_names_the_actual_operation(capture, operation, expected) -> None:
    """재시도도 어느 작업이 흔들리는지 문구에서 바로 보여야 한다."""

    emit_event(
        OperationalEvent.DEPENDENCY_REQUEST_RETRY,
        outcome=EventOutcome.FAILURE,
        level=logging.WARNING,
        dependency="app-server",
        operation=operation,
        attempt=1,
        maxAttempts=3,
        reason="server_error",
        httpStatus=503,
    )

    assert _events(capture)[-1]["message"] == expected


@pytest.mark.parametrize(
    ("dependency", "operation"),
    [
        # 앞으로 추가될 operation
        ("app-server", "timeline-summary"),
        # 앞으로 추가될 dependency
        ("search-index", "input"),
        # 값이 오염된 경우. 이런 것이 문구가 되면 message 가 곧 유출 통로다.
        ("app-server", "result?taskToken=tok-secret"),
        ("app-server", ""),
    ],
)
def test_unknown_labels_fall_back_without_reaching_the_message(
    capture, dependency, operation
) -> None:
    """라벨을 모르는 값은 문구에 **넣지 않고** 통째로 일반 문구로 폴백한다."""

    emit_event(
        OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
        outcome=EventOutcome.SUCCESS,
        dependency=dependency,
        operation=operation,
        httpStatus=200,
    )

    payload = _events(capture)[-1]
    assert payload["message"] == "외부 연동 호출 완료"
    assert dependency not in payload["message"]
    if operation:
        assert operation not in payload["message"]
    # 구조화 필드로는 그대로 나간다. 문구만 폴백이고 집계 계약은 손대지 않는다.
    assert payload["operation"] == operation
    assert payload["dependency"] == dependency


def test_message_wording_does_not_move_the_structured_contract(capture) -> None:
    """문구가 구체화돼도 `event.action`·`event.outcome`·필드·건수는 그대로다."""

    emit_event(
        OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
        outcome=EventOutcome.FAILURE,
        level=logging.ERROR,
        dependency="app-server",
        operation="result",
        httpStatus=500,
        attempts=3,
        durationMs=980.5,
        errorCode=1203,
        taskId="task-9",
        tokenRefreshCount=1,
    )

    events = _events(capture)
    assert len(events) == 1, "문구를 바꿔도 논리 호출 하나는 이벤트 한 건이다."
    payload = events[0]
    assert payload[ACTION_FIELD] == OperationalEvent.DEPENDENCY_REQUEST_COMPLETED.value
    assert payload[OUTCOME_FIELD] == EventOutcome.FAILURE.value
    assert payload["dependency"] == "app-server"
    assert payload["operation"] == "result"
    assert payload["httpStatus"] == 500
    assert payload["attempts"] == 3
    assert payload["errorCode"] == 1203
    assert payload["taskId"] == "task-9"
    assert payload["message"] == "App Server 타임라인 결과 저장 실패"


def test_message_never_carries_the_url_or_the_token(capture) -> None:
    """문구는 고정 라벨 조합이라 고카디널리티·민감값이 들어갈 자리가 없다."""

    emit_event(
        OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
        outcome=EventOutcome.SUCCESS,
        dependency="app-server",
        operation="input",
        httpStatus=200,
        # 허용 목록 밖이라 버려지는 값들. 문구에도 절대 나타나지 않는다.
        url="https://app.example/s/api/v1/timelines/task-1/input",
        taskToken="tok-secret",
    )

    payload = _events(capture)[-1]
    assert payload["message"] == "App Server 타임라인 입력 조회 성공"
    serialized = json.dumps(payload, ensure_ascii=False)
    for leaked in ("app.example", "tok-secret", "task-1/input"):
        assert leaked not in serialized


def test_odd_label_values_fall_back_without_losing_the_event(capture) -> None:
    """문구를 고르다 터져서 이벤트가 통째로 사라지면 안 된다."""

    emit_event(
        OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
        outcome=EventOutcome.SUCCESS,
        dependency=["app-server"],  # 해시할 수 없는 값
        operation=7,
        httpStatus=200,
    )

    events = _events(capture)
    assert len(events) == 1
    assert events[0]["message"] == "외부 연동 호출 완료"
    assert events[0][ACTION_FIELD] == OperationalEvent.DEPENDENCY_REQUEST_COMPLETED.value


# --- 저하 이벤트 (이슈 #101) -------------------------------------------------
#
# `app.degraded` 는 **작업이 성공으로 끝나도** 나가는 유일한 이벤트다. 흡수 경계가
# 예외를 삼키고 fallback 으로 진행하므로 완료 이벤트만으로는 무엇을 잃었는지 알 수
# 없다. 여기서 지키는 것은 셋이다.
#
# 1. 표식·레벨·outcome 이 계약대로 나간다.
# 2. component/agent 를 안 주면 실행 컨텍스트에서 온다.
# 3. LLM 진단은 정해진 이름만 실리고 그 밖의 값은 통째로 버려진다.


def test_degraded_event_carries_the_marker_at_warning_level(capture) -> None:
    emit_degraded(
        DegradedComponent.LANGFUSE,
        error_code=1408,
        error_type="ClientError",
    )

    events = _events(capture)
    assert len(events) == 1
    event = events[0]
    assert event[DATASET_FIELD] == EVENT_DATASET
    assert event[ACTION_FIELD] == OperationalEvent.APP_DEGRADED.value
    assert event[OUTCOME_FIELD] == EventOutcome.FAILURE.value
    # 저하는 실패지만 작업을 죽이지 않는다. ERROR 로 올리면 실제 실패와 섞인다.
    assert event["log.level"] == "WARNING"
    assert event["component"] == "langfuse"
    assert event["errorCode"] == 1408
    assert event["errorType"] == "ClientError"


def test_degraded_event_takes_component_and_agent_from_the_context(capture) -> None:
    """흡수 경계는 자기가 어느 단계인지 인자로 말하지 않는다."""

    with execution_context("task-9"):
        with execution_scope(ExecutionStage.EVENT_AGENT, agent="photo"):
            emit_degraded()

    event = _events(capture)[0]
    assert event["component"] == ExecutionStage.EVENT_AGENT.value
    # 나가는 이름은 `agent` 가 아니다(#109). 그 이름은 Filebeat 의 수집기 객체 몫이다.
    assert event["agentName"] == "photo"
    assert "agent" not in event
    assert event["taskId"] == "task-9"


def test_degraded_event_keeps_only_the_declared_llm_trace_fields(capture) -> None:
    """`trace_fields` 를 통째로 펴지 않는다. 그 자리가 곧 콘텐츠 통로가 된다."""

    emit_degraded(
        ExecutionStage.LLM,
        error_code=1202,
        provider="bedrock",
        model="global.amazon.nova-2-lite-v1:0",
        provider_version="1.40.0",
        trace_fields={
            "stopReason": "max_tokens",
            "contentBlockKinds": ["text"],
            "tokenUsage": {"inputTokens": 12},
            # 아래는 계약에 없다. 응답 본문이 이 경로로 들어오면 안 된다.
            "responseText": "사용자의 하루 일기 본문…",
            "prompt": "system prompt…",
        },
    )

    event = _events(capture)[0]
    assert event["provider"] == "bedrock"
    assert event["model"] == "global.amazon.nova-2-lite-v1:0"
    assert event["providerVersion"] == "1.40.0"
    assert event["stopReason"] == "max_tokens"
    assert event["contentBlockKinds"] == ["text"]
    assert event["tokenUsage"] == {"inputTokens": 12}
    serialized = json.dumps(event, ensure_ascii=False)
    for leaked in ("responseText", "사용자의 하루 일기 본문", "prompt", "system prompt"):
        assert leaked not in serialized
