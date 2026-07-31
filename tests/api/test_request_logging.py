"""요청 로그 미들웨어 검증 (이슈 #47).

uvicorn access log 를 대신하므로, 여기서 남기지 않으면 요청 기록 자체가 사라진다.
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.request_logging import RequestLoggingMiddleware

LOGGER_NAME = "app.api.request_logging"


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/v1/thing")
    def thing():
        return {"ok": True}

    @app.get("/ping")
    def ping():
        return {"status": "Healthy"}

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    return app


def _request_records(caplog) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if record.name == LOGGER_NAME
    ]


def test_logs_method_path_status_and_duration(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        with TestClient(_app()) as client:
            client.get("/v1/thing")

    record = _request_records(caplog)[-1]
    assert record.levelno == logging.INFO
    assert record.getMessage() == "요청 처리 완료"
    assert record.fields["method"] == "GET"
    assert record.fields["path"] == "/v1/thing"
    assert record.fields["httpStatus"] == 200
    assert record.fields["durationMs"] >= 0


def test_query_string_is_not_logged(caplog) -> None:
    """경로와 달리 쿼리에는 값이 실릴 수 있다."""

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        with TestClient(_app()) as client:
            client.get("/v1/thing?token=secret-value&q=사용자입력")

    fields = _request_records(caplog)[-1].fields
    assert fields["path"] == "/v1/thing"
    assert "secret-value" not in str(fields)
    assert "사용자입력" not in str(fields)


def test_health_check_polling_stays_at_debug(caplog) -> None:
    """배포 스크립트가 수 초마다 두드리는 경로다. INFO 면 로그가 이걸로만 찬다."""

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        with TestClient(_app()) as client:
            client.get("/ping")

    assert _request_records(caplog)[-1].levelno == logging.DEBUG


def test_error_status_raises_the_log_level(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        with TestClient(_app(), raise_server_exceptions=False) as client:
            client.get("/boom")

    record = _request_records(caplog)[-1]
    assert record.levelno == logging.ERROR
    assert record.fields["httpStatus"] == 500


def test_unhandled_exception_still_closes_the_request_line(caplog) -> None:
    """응답을 시작하지도 못한 경우에도 요청 줄은 남아야 한다."""

    app = _app()

    class _Explode:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("path") == "/explode":
                raise RuntimeError("응답 시작 전 실패")
            await self.inner(scope, receive, send)

    wrapped = RequestLoggingMiddleware(_Explode(app))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        with TestClient(wrapped, raise_server_exceptions=False) as client:
            client.get("/explode")

    record = _request_records(caplog)[-1]
    assert record.levelno == logging.ERROR
    assert record.fields["path"] == "/explode"
    assert record.fields["httpStatus"] == 500
