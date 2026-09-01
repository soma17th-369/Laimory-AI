"""요청 운영 이벤트 검증 (이슈 #47, #53).

uvicorn access log 를 대신하므로 여기서 남기지 않으면 요청 기록 자체가 사라진다.
그리고 실패한 요청도 이벤트는 **한 건**이어야 한다 — 처리기와 미들웨어가 각자
남기면 같은 요청이 두 줄로, 레벨도 서로 다르게 집계된다.
"""

import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.error_handlers import register_error_handlers
from app.api.request_logging import RequestLoggingMiddleware, annotate_request_task
from app.core.error_codes import ErrorCode
from app.core.logging import JsonLogFormatter
from app.core.operational_logging import (
    ACTION_FIELD,
    DATASET_FIELD,
    EVENT_DATASET,
    OUTCOME_FIELD,
    EventOutcome,
    OperationalEvent,
)

OPERATIONAL_LOGGER = "app.operational"


class _Payload(BaseModel):
    taskId: str


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    register_error_handlers(app)

    @app.get("/v1/thing/{thing_id}")
    def thing(thing_id: str):
        return {"ok": thing_id}

    @app.get("/ping")
    def ping():
        return {"status": "Healthy"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/unstable")
    def unstable():
        raise RuntimeError("내부 경로 /srv/secret 실패, token=abc123")

    @app.get("/conflict")
    def conflict():
        raise HTTPException(status_code=409, detail="Conflict")

    @app.post("/v1/accept")
    def accept(payload: _Payload, request: Request):
        annotate_request_task(request, payload.taskId)
        return {"taskId": payload.taskId}

    return app


def _events(caplog) -> list[dict]:
    formatter = JsonLogFormatter()
    return [
        json.loads(formatter.format(record))
        for record in caplog.records
        if record.name == OPERATIONAL_LOGGER
        and getattr(record, "operational_event", {}).get(ACTION_FIELD)
        == OperationalEvent.HTTP_REQUEST_COMPLETED.value
    ]


def _records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == OPERATIONAL_LOGGER]


def test_successful_request_is_one_event_with_route_status_and_duration(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()) as client:
            client.get("/v1/thing/abc")

    events = _events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event[DATASET_FIELD] == EVENT_DATASET
    assert event[OUTCOME_FIELD] == EventOutcome.SUCCESS.value
    assert event["log.level"] == "INFO"
    assert event["method"] == "GET"
    # 실제 path(`/v1/thing/abc`)가 아니라 라우트 템플릿이다. 경로 파라미터는 값이다.
    assert event["route"] == "/v1/thing/{thing_id}"
    assert event["httpStatus"] == 200
    assert event["durationMs"] >= 0
    assert "errorCode" not in event


def test_query_string_and_path_parameter_values_are_not_logged(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()) as client:
            client.get("/v1/thing/사용자값?token=secret-value&q=사용자입력")

    serialized = json.dumps(_events(caplog)[-1], ensure_ascii=False)
    assert "secret-value" not in serialized
    assert "사용자입력" not in serialized
    assert "사용자값" not in serialized


def test_health_check_polling_is_not_collected(caplog) -> None:
    """수 초마다 두드리는 경로다. 적재하면 운영 로그가 이걸로만 찬다."""

    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()) as client:
            client.get("/ping")
            client.get("/health")

    assert _events(caplog) == []


def test_validation_failure_is_one_warning_event_with_the_error_code(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()) as client:
            client.post("/v1/accept", json={"taskId": 1234, "extra": "사용자입력"})

    events = _events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["httpStatus"] == 422
    assert event["log.level"] == "WARNING"
    assert event[OUTCOME_FIELD] == EventOutcome.FAILURE.value
    assert event["errorCode"] == int(ErrorCode.REQUEST_VALIDATION_FAILED)
    assert event["errorType"] == "RequestValidationError"
    assert "사용자입력" not in json.dumps(event, ensure_ascii=False)


def test_unmatched_path_is_recorded_as_404_with_a_safe_path(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()) as client:
            client.get("/v1/does-not-exist")

    event = _events(caplog)[-1]
    assert event["httpStatus"] == 404
    assert event["route"] == "/v1/does-not-exist"
    assert event["errorCode"] == int(ErrorCode.NOT_FOUND)
    assert event["log.level"] == "WARNING"


def test_http_exception_keeps_its_status_and_code(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(_app(), raise_server_exceptions=False) as client:
            client.get("/conflict")

    event = _events(caplog)[-1]
    assert event["httpStatus"] == 409
    assert event["errorCode"] == int(ErrorCode.BAD_REQUEST)


def test_unhandled_exception_is_one_error_event_with_internal_code(caplog) -> None:
    """미분류 예외 처리기는 이 미들웨어 바깥에서 돌아 주석이 닿지 못한다."""

    with caplog.at_level(logging.DEBUG):
        with TestClient(_app(), raise_server_exceptions=False) as client:
            client.get("/v1/unstable")

    events = _events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["httpStatus"] == 500
    assert event["log.level"] == "ERROR"
    assert event["errorCode"] == int(ErrorCode.INTERNAL_ERROR)
    assert event["errorType"] == "RuntimeError"


def test_unhandled_exception_carries_its_cause_to_elasticsearch(caplog) -> None:
    """미분류 500 은 코드만으로 원인을 알 수 없다(#109 범위 확장).

    처리기의 주석이 도착하지 못하는 경로라, 미들웨어가 잡은 예외에서 원문과 traceback 을
    직접 채우지 않으면 운영에서 원인을 볼 수단이 아예 없다. **여기 실리는 값에는 사용자
    콘텐츠가 섞일 수 있다** — 알고 연 경계이므로 그 사실을 테스트로도 드러내 둔다.
    """

    with caplog.at_level(logging.DEBUG):
        with TestClient(_app(), raise_server_exceptions=False) as client:
            client.get("/v1/unstable")

    event = _events(caplog)[0]
    assert "/srv/secret" in event["errorMessage"]
    assert "Traceback" in event["errorStackTrace"]
    assert "RuntimeError" in event["errorStackTrace"]


def test_accepted_task_id_correlates_the_request_with_the_background_work(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()) as client:
            client.post("/v1/accept", json={"taskId": "task-42"})

    event = _events(caplog)[-1]
    assert event["taskId"] == "task-42"
    assert event["route"] == "/v1/accept"


def test_failure_before_the_response_starts_still_closes_the_request(caplog) -> None:
    app = _app()

    class _Explode:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("path") == "/explode":
                raise RuntimeError("응답 시작 전 실패")
            await self.inner(scope, receive, send)

    wrapped = RequestLoggingMiddleware(_Explode(app))

    with caplog.at_level(logging.DEBUG):
        with TestClient(wrapped, raise_server_exceptions=False) as client:
            client.get("/explode")

    event = _events(caplog)[-1]
    assert event["httpStatus"] == 500
    assert event["route"] == "/explode"
    assert event["errorCode"] == int(ErrorCode.INTERNAL_ERROR)


def test_no_operational_event_is_emitted_for_non_http_scopes(caplog) -> None:
    """lifespan 같은 다른 scope 는 요청이 아니다."""

    with caplog.at_level(logging.DEBUG):
        with TestClient(_app()):
            pass

    assert [
        record
        for record in _records(caplog)
        if getattr(record, "operational_event", {}).get(ACTION_FIELD)
        == OperationalEvent.HTTP_REQUEST_COMPLETED.value
    ] == []
