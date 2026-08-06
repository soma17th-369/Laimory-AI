"""백그라운드 User Memory 갱신 처리 (#64).

이 파일의 계약은 하나로 요약된다. **어떤 경로로 끝나든 결과 저장 호출이 정확히
1회다.** 완료 콜백이 없으므로 그 호출을 빠뜨리면 실패를 알릴 수단이 없고, App Server
작업은 TTL 까지 매달린다.
"""

import asyncio
import logging
import time

import pytest

from app.core.error_codes import ErrorCode, message_for
from app.core.operational_logging import OperationalEvent
from app.core.structured import StructuredOutputError
from app.schemas import TaskStatus
from app.schemas.user_memory import UserMemory
from app.services import user_memory_runner
from app.services.app_server_client import AppServerError
from app.services.user_memory_limits import MAX_DAILY_TIMELINE_COUNT
from app.services.user_memory_repair import UserMemoryLimitError
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.user_memory import (
    TASK_ID,
    TASK_TOKEN,
    daily_timeline,
    daily_timeline_event,
    memory_body,
    update_request,
)


class _StubAgent:
    """준비한 메모리를 돌려주거나 준비한 예외를 던진다."""

    def __init__(self, result=None, *, delay_sec: float = 0.0) -> None:
        self._result = result if result is not None else UserMemory(
            basic_profile="30대 개발자입니다."
        )
        self._delay_sec = delay_sec
        self.calls: list[tuple] = []

    def generate(self, existing, digest, *, violations=()):
        self.calls.append((existing, digest, list(violations)))
        if self._delay_sec:
            time.sleep(self._delay_sec)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _run(
    client: FakeAppServerClient,
    agent: _StubAgent | None = None,
    **request_overrides,
) -> TaskStatus:
    payload = update_request(**request_overrides)
    return asyncio.run(
        user_memory_runner.process_user_memory_task(
            payload.task_id,
            client,
            payload,
            agent or _StubAgent(),
        )
    )


def _events(caplog) -> list[dict]:
    return [
        payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
        and payload.get("event.action")
        == OperationalEvent.USER_MEMORY_TASK_COMPLETED.value
    ]


# --- 정상 경로 ---------------------------------------------------------


def test_success_sends_the_updated_memory_exactly_once():
    client = FakeAppServerClient()

    status = _run(client)

    assert status is TaskStatus.SUCCESS
    assert client.order == ["user-memory-result"]
    result = client.last_user_memory
    assert result.status is TaskStatus.SUCCESS
    assert result.user_memory.basic_profile == "30대 개발자입니다."
    assert result.error_code is None


def test_result_call_uses_the_accepted_token():
    """토큰은 갱신되지 않는다. 호출이 하나라 갱신될 기회 자체가 없다."""

    client = FakeAppServerClient()

    _run(client)

    assert client.user_memory_calls[0].token == TASK_TOKEN
    assert client.user_memory_calls[0].task_id == TASK_ID


def test_server_stamps_the_schema_version_and_updated_at():
    client = FakeAppServerClient()

    _run(client)

    memory = client.last_user_memory.user_memory
    assert memory.schema_version == "1.0"
    assert memory.updated_at is not None


def test_first_time_creation_needs_no_existing_memory():
    client = FakeAppServerClient()
    agent = _StubAgent()

    status = _run(client, agent, userMemory=None)

    assert status is TaskStatus.SUCCESS
    assert agent.calls[0][0] is None


def test_existing_memory_is_handed_to_the_agent():
    client = FakeAppServerClient()
    agent = _StubAgent()

    _run(client, agent, userMemory=memory_body(basicProfile="20대 학생입니다."))

    assert agent.calls[0][0].basic_profile == "20대 학생입니다."


def test_a_day_without_memo_still_succeeds():
    """성향 필드가 안 바뀌는 것이 정상이다. 실패가 아니다."""

    client = FakeAppServerClient()
    agent = _StubAgent()

    status = _run(
        client,
        agent,
        dailyTimelines=[daily_timeline(events=[daily_timeline_event(memo=None)])],
    )

    assert status is TaskStatus.SUCCESS
    assert agent.calls[0][1].has_memo is False


# --- 기존 프로필 계약 위반 (흡수) --------------------------------------


def test_unreadable_existing_memory_is_absorbed_and_rebuilt(caplog):
    """여기서 멈추면 그 사용자는 이후 어떤 날도 갱신되지 않는다."""

    client = FakeAppServerClient()
    agent = _StubAgent()

    with caplog.at_level(logging.DEBUG):
        status = _run(client, agent, userMemory={"favoriteColor": "파랑"})

    assert status is TaskStatus.SUCCESS
    assert agent.calls[0][0] is None
    codes = {
        getattr(record, "fields", {}).get("errorCode") for record in caplog.records
    }
    assert int(ErrorCode.USER_MEMORY_CONTRACT_VIOLATION) in codes


def test_absorbed_existing_memory_failure_does_not_log_the_body(caplog):
    """``str(ValidationError)`` 는 걸린 값을 ``input_value=...`` 로 인용한다."""

    client = FakeAppServerClient()

    with caplog.at_level(logging.DEBUG):
        _run(client, userMemory={"basicProfile": "비밀번호는 hunter2 입니다"})

    assert "hunter2" not in caplog.text


# --- 실패 경로: 전부 결과 저장 1회로 수렴한다 --------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            StructuredOutputError("스키마 실패"),
            ErrorCode.STRUCTURED_OUTPUT_INVALID,
            id="structured-output",
        ),
        pytest.param(
            UserMemoryLimitError("상한 초과"),
            ErrorCode.USER_MEMORY_LIMIT_EXCEEDED,
            id="limit",
        ),
        pytest.param(
            RuntimeError("알 수 없음"),
            ErrorCode.USER_MEMORY_GENERATION_FAILED,
            id="unclassified",
        ),
    ],
)
def test_generation_failure_is_reported_through_the_same_call(error, expected):
    client = FakeAppServerClient()

    status = _run(client, _StubAgent(error))

    assert status is TaskStatus.FAILED
    assert len(client.user_memory_calls) == 1
    result = client.last_user_memory
    assert result.status is TaskStatus.FAILED
    assert result.error_code == int(expected)
    assert result.error == message_for(expected)
    # 실패에는 부분 결과를 싣지 않는다.
    assert result.user_memory is None


def test_timeout_still_reports_failure(monkeypatch):
    """예산을 넘겨도 통보는 나간다. 안 그러면 App Server 가 TTL 까지 매달린다."""

    monkeypatch.setattr(
        user_memory_runner.settings, "user_memory_timeout_sec", 0.05
    )
    client = FakeAppServerClient()

    status = _run(client, _StubAgent(delay_sec=0.5))

    assert status is TaskStatus.FAILED
    assert len(client.user_memory_calls) == 1
    assert client.last_user_memory.error_code == int(
        ErrorCode.USER_MEMORY_GENERATION_FAILED
    )


def test_result_call_failure_is_still_a_single_attempt():
    """"시도했지만 못 보냈다" 와 "아예 보내지 않았다" 는 다른 사고다."""

    client = FakeAppServerClient(
        user_memory_error=AppServerError(
            "저장 실패", code=ErrorCode.USER_MEMORY_SUBMIT_FAILED
        )
    )

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert len(client.user_memory_calls) == 1


def test_aborting_status_does_not_trigger_a_retry_call():
    client = FakeAppServerClient(
        user_memory_error=AppServerError(
            "토큰 거절", code=ErrorCode.APP_SERVER_UNAUTHORIZED, abort=True
        )
    )

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert len(client.user_memory_calls) == 1


# --- 관측 -------------------------------------------------------------


def test_success_closes_the_task_with_one_operational_event(caplog):
    with caplog.at_level(logging.DEBUG):
        status = _run(FakeAppServerClient())

    assert status is TaskStatus.SUCCESS
    events = _events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["event.outcome"] == "success"
    assert event["taskId"] == TASK_ID
    assert event["status"] == TaskStatus.SUCCESS.value
    assert event["resultSent"] is True
    assert event["schemaVersion"] == "1.0"
    assert event["repairAttempts"] == 0
    assert event["durationMs"] >= 0
    assert "errorCode" not in event


def test_failed_result_call_is_visible_in_the_event(caplog):
    client = FakeAppServerClient(
        user_memory_error=AppServerError(
            "저장 실패", code=ErrorCode.USER_MEMORY_SUBMIT_FAILED
        )
    )

    with caplog.at_level(logging.DEBUG):
        _run(client)

    event = _events(caplog)[-1]
    assert event["resultSent"] is False
    assert event["errorCode"] == int(ErrorCode.USER_MEMORY_SUBMIT_FAILED)


def test_event_reports_what_was_dropped_from_the_input(caplog):
    """조용히 자르면 "다 보고 이 정도" 인지 "못 본 게 있어서" 인지 알 수 없다."""

    daily_timelines = [
        daily_timeline(record_date=f"2026-07-{day:02d}")
        for day in range(1, 12)
    ]

    with caplog.at_level(logging.DEBUG):
        _run(FakeAppServerClient(), dailyTimelines=daily_timelines)

    event = _events(caplog)[-1]
    assert event["dailyTimelineCount"] == MAX_DAILY_TIMELINE_COUNT
    assert event["droppedDailyTimelineCount"] == 11 - MAX_DAILY_TIMELINE_COUNT


def test_event_never_carries_timeline_or_memory_content(caplog):
    with caplog.at_level(logging.DEBUG):
        _run(
            FakeAppServerClient(),
            _StubAgent(UserMemory(basic_profile="비밀 프로필 문장")),
            dailyTimelines=[
                daily_timeline(events=[daily_timeline_event(title="비밀 제목", memo="비밀 메모")])
            ],
        )

    event = _events(caplog)[-1]
    serialized = str(event)
    assert "비밀 제목" not in serialized
    assert "비밀 메모" not in serialized
    assert "비밀 프로필 문장" not in serialized
    assert TASK_TOKEN not in serialized


def test_task_token_never_appears_in_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        _run(FakeAppServerClient(), taskToken="tok-secret")

    assert "tok-secret" not in caplog.text
