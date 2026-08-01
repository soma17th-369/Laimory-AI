"""서버 수명주기 운영 이벤트 검증 (이슈 #53).

컨테이너가 언제 올라오고 언제 내려갔는지를 모르면 "요청이 끊긴 구간" 을 배포
때문인지 장애 때문인지 구분할 수 없다.
"""

import logging

from fastapi.testclient import TestClient

from app.core.operational_logging import OperationalEvent
from app.server import app


def _actions(caplog) -> list[str]:
    return [
        payload["event.action"]
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
    ]


def test_startup_and_shutdown_are_recorded(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(app):
            pass

    actions = _actions(caplog)
    assert OperationalEvent.SERVER_STARTED.value in actions
    assert OperationalEvent.SERVER_STOPPED.value in actions
    assert actions.index(OperationalEvent.SERVER_STARTED.value) < actions.index(
        OperationalEvent.SERVER_STOPPED.value
    )


def test_lifecycle_events_carry_only_environment_and_uptime(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(app):
            pass

    payloads = {
        payload["event.action"]: payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
    }

    started = payloads[OperationalEvent.SERVER_STARTED.value]
    assert set(started) == {
        "event.dataset",
        "event.action",
        "event.outcome",
        "appEnv",
        "logFormat",
    }

    stopped = payloads[OperationalEvent.SERVER_STOPPED.value]
    assert stopped["uptimeMs"] >= 0
    assert set(stopped) == {
        "event.dataset",
        "event.action",
        "event.outcome",
        "appEnv",
        "uptimeMs",
    }
