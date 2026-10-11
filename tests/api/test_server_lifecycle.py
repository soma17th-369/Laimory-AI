"""서버 수명주기 운영 이벤트 검증 (이슈 #53).

컨테이너가 언제 올라오고 언제 내려갔는지를 모르면 "요청이 끊긴 구간" 을 배포
때문인지 장애 때문인지 구분할 수 없다.
"""

import logging

from fastapi.testclient import TestClient

from app.core.operational_logging import INSTANCE_ID, OperationalEvent
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
        "instanceId",
    }

    stopped = payloads[OperationalEvent.SERVER_STOPPED.value]
    assert stopped["uptimeMs"] >= 0
    assert set(stopped) == {
        "event.dataset",
        "event.action",
        "event.outcome",
        "appEnv",
        "uptimeMs",
        "instanceId",
    }


def test_lifecycle_events_share_one_instance_id(caplog) -> None:
    """기동과 종료가 같은 `instanceId` 를 갖는다 (이슈 #101).

    AgentCore 는 한 log group 에 여러 컨테이너 인스턴스의 줄을 섞어 보낸다. 이 값이
    흔들리면 어느 기동에 어느 종료가 짝인지 알 수 없고 cold start 도 셀 수 없다.
    """

    with caplog.at_level(logging.DEBUG):
        with TestClient(app):
            pass

    payloads = {
        payload["event.action"]: payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
    }

    started = payloads[OperationalEvent.SERVER_STARTED.value]["instanceId"]
    stopped = payloads[OperationalEvent.SERVER_STOPPED.value]["instanceId"]
    assert started == stopped == INSTANCE_ID
    # 호출부가 넘긴 값이 아니라 프로세스의 성질이다. `app/server.py` 는 이 값을 모른다.
    assert len(started) == 36
