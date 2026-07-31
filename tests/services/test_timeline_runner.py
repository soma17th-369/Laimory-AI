"""백그라운드 타임라인 처리와 App Server 연동을 검증한다.

이슈 #40 이후 데이터 경로는 App Server API 하나다: 입력 조회 → 추론 → 결과 저장
→ 콜백. 순서와 실패 분류, 토큰 취급이 이 파일의 계약이다.
"""

import asyncio
import logging

from app.core.error_codes import ErrorCode, message_for
from app.core.execution_context import ExecutionStage
from app.core.structured import StructuredOutputError
from app.schemas import TaskStatus, TimelineDraft
from app.services import timeline_runner
from app.services.app_server_client import AppServerError
from app.services.timeline_validator import (
    TimelineValidationError,
    TimelineViolation,
    TimelineViolationCode,
)
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.requests import default_source_items, fixture_raw_id, make_snapshot

_TASK_ID = "task-1"
_TASK_TOKEN = "task-token-1"
_DAILY_RECORD_ID = 42
_WINDOW_START = "2026-06-20T00:00:00+09:00"
_WINDOW_END = "2026-06-21T00:00:00+09:00"


def _draft() -> TimelineDraft:
    return TimelineDraft(user_id="u-1", date="2026-06-20", timezone="Asia/Seoul")


def _client(**overrides) -> FakeAppServerClient:
    defaults = dict(
        snapshot=make_snapshot(task_id=_TASK_ID, source_items=default_source_items())
    )
    defaults.update(overrides)
    return FakeAppServerClient(**defaults)


def _run(client: FakeAppServerClient, task_token: str = _TASK_TOKEN) -> TaskStatus:
    return asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID,
            client,
            _DAILY_RECORD_ID,
            _WINDOW_START,
            _WINDOW_END,
            task_token,
        )
    )


def _patch_agent(monkeypatch, draft: TimelineDraft | None = None) -> None:
    async def fake_main_agent(request):
        return draft if draft is not None else _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)


def test_success_stores_result_then_sends_success_callback(monkeypatch):
    _patch_agent(monkeypatch)
    client = _client()

    status = _run(client)

    assert status is TaskStatus.SUCCESS
    # 순서가 계약이다. 저장 200 을 확인하기 전에 SUCCESS 를 통보하면 App Server 가
    # 아직 없는 결과를 읽으러 간다.
    assert client.order == ["input", "result", "callback"]
    assert client.last_callback.status is TaskStatus.SUCCESS
    assert client.last_callback.error_code is None
    assert client.last_callback.error is None


def test_all_calls_use_the_same_task_token(monkeypatch):
    _patch_agent(monkeypatch)
    client = _client()

    _run(client, task_token="tok-abc")

    assert [call.token for call in client.fetch_calls] == ["tok-abc"]
    assert [call.token for call in client.submit_calls] == ["tok-abc"]
    assert [call.token for call in client.callback_calls] == ["tok-abc"]


def test_refreshed_token_from_response_is_used_by_later_calls(monkeypatch):
    """응답 body 가 새 토큰을 주면 그 뒤 호출은 갱신된 값을 쓴다."""

    _patch_agent(monkeypatch)
    client = _client(fetch_token="tok-2", submit_token="tok-3")

    _run(client, task_token="tok-1")

    assert client.fetch_calls[0].token == "tok-1"
    assert client.submit_calls[0].token == "tok-2"
    assert client.callback_calls[0].token == "tok-3"


def test_task_token_never_appears_in_logs(monkeypatch, caplog):
    """토큰은 로그 금지 대상이다(계약)."""

    _patch_agent(monkeypatch)
    client = _client(fetch_token="tok-rotated")

    with caplog.at_level(logging.DEBUG):
        status = _run(client, task_token="tok-secret")

    assert status is TaskStatus.SUCCESS
    assert "tok-secret" not in caplog.text
    assert "tok-rotated" not in caplog.text


def test_logs_stage_boundaries_for_operational_tracing(monkeypatch, caplog):
    """운영자가 taskId 로 단계별 소요시간과 진행 지점을 따라갈 수 있어야 한다."""

    _patch_agent(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.services.timeline_runner"):
        status = _run(_client())

    assert status is TaskStatus.SUCCESS
    messages = [record.getMessage() for record in caplog.records]
    assert f"단계 시작: {ExecutionStage.REQUEST.value}" in messages
    assert f"단계 완료: {ExecutionStage.REQUEST.value}" in messages
    assert f"단계 시작: {ExecutionStage.STORAGE.value}" in messages
    assert f"단계 완료: {ExecutionStage.STORAGE.value}" in messages

    storage_done = next(
        record
        for record in caplog.records
        if record.getMessage() == f"단계 완료: {ExecutionStage.STORAGE.value}"
    )
    assert storage_done.fields["durationMs"] >= 0

    final = next(
        record
        for record in caplog.records
        if record.getMessage() == "타임라인 처리 종료"
    )
    assert final.fields["stage"] == ExecutionStage.FINAL.value
    assert final.fields["status"] == TaskStatus.SUCCESS.value
    assert final.fields["callbackSent"] is True
    assert "errorCode" not in final.fields


def test_missing_input_fails_without_callback(monkeypatch):
    """입력 조회 404 는 task 가 없다는 뜻이라 콜백도 거절된다 — 보내지 않는다."""

    _patch_agent(monkeypatch)
    client = FakeAppServerClient(snapshot=None)

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.callback_calls == []
    assert client.submit_calls == []


def test_unauthorized_token_aborts_without_callback(monkeypatch):
    _patch_agent(monkeypatch)
    client = _client(
        fetch_error=AppServerError(
            "401",
            code=ErrorCode.APP_SERVER_UNAUTHORIZED,
            abort=True,
        )
    )

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.callback_calls == []


def test_input_fetch_failure_sends_failed_callback(monkeypatch):
    """5xx/timeout 소진은 task 가 살아 있으므로 실패를 통보한다."""

    _patch_agent(monkeypatch)
    client = _client(
        fetch_error=AppServerError(
            "재시도 소진",
            code=ErrorCode.SOURCE_FETCH_FAILED,
        )
    )

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.last_callback.error_code == int(ErrorCode.SOURCE_FETCH_FAILED)
    assert client.last_callback.error == message_for(ErrorCode.SOURCE_FETCH_FAILED)


def test_agent_exception_returns_failed_callback(monkeypatch):
    async def boom(request):
        raise RuntimeError("메인 에이전트 오류")

    monkeypatch.setattr(timeline_runner, "run_main_agent", boom)
    client = _client()

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.submit_calls == []
    # 분류되지 않은 예외라 INTERNAL_ERROR 다. 원본 메시지("메인 에이전트 오류")는
    # 로그에만 남고 콜백에는 카탈로그 안전 메시지가 나간다.
    assert client.last_callback.error_code == int(ErrorCode.INTERNAL_ERROR)
    assert client.last_callback.error == message_for(ErrorCode.INTERNAL_ERROR)
    assert "메인 에이전트 오류" not in (client.last_callback.error or "")


def test_structured_output_failure_keeps_specific_callback_code(monkeypatch):
    async def boom(request):
        raise StructuredOutputError("rawId=내부값이 잘못됐습니다")

    monkeypatch.setattr(timeline_runner, "run_main_agent", boom)
    client = _client()

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.last_callback.error_code == int(ErrorCode.STRUCTURED_OUTPUT_INVALID)
    assert "rawId" not in (client.last_callback.error or "")


def test_result_submit_failure_sends_failed_callback(monkeypatch):
    """저장 성공 **전** 이므로 FAILED 통보가 허용된다."""

    _patch_agent(monkeypatch)
    client = _client(
        submit_error=AppServerError(
            "재시도 소진",
            code=ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED,
        )
    )

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.last_callback.status is TaskStatus.FAILED
    assert client.last_callback.error_code == int(
        ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED
    )


def test_result_conflict_aborts_without_callback(monkeypatch):
    _patch_agent(monkeypatch)
    client = _client(
        submit_error=AppServerError(
            "409",
            code=ErrorCode.APP_SERVER_CONFLICT,
            abort=True,
        )
    )

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.callback_calls == []


def test_callback_send_failure_does_not_change_status(monkeypatch):
    """저장이 끝난 뒤의 콜백 실패는 결과를 되돌리지 않는다."""

    _patch_agent(monkeypatch)
    client = _client(callback_ok=False)

    status = _run(client)

    assert status is TaskStatus.SUCCESS
    assert len(client.submit_calls) == 1


def test_storage_validation_failure_logs_safe_codes_without_raw_id(
    monkeypatch,
    caplog,
):
    _patch_agent(monkeypatch)

    unknown_raw_id = fixture_raw_id("runner-unknown")
    validation_error = TimelineValidationError(
        [
            TimelineViolation(
                TimelineViolationCode.SOURCE_RAW_ID_NOT_IN_TASK,
                f"source rawId={unknown_raw_id} 가 현재 task 입력에 없습니다",
            )
        ]
    )
    monkeypatch.setattr(
        timeline_runner,
        "ensure_timeline_valid_for_storage",
        _raise(validation_error),
    )

    client = _client()
    with caplog.at_level(logging.WARNING, logger="app.services.timeline_runner"):
        status = _run(client)

    assert status is TaskStatus.FAILED
    # 검증에서 걸렸으므로 저장 요청 자체를 보내지 않는다.
    assert client.submit_calls == []
    failure = next(
        record
        for record in caplog.records
        if record.getMessage() == "타임라인 처리 실패"
    )
    assert failure.fields["errorCode"] == int(
        ErrorCode.TIMELINE_STORAGE_VALIDATION_FAILED
    )
    assert failure.fields["stage"] == ExecutionStage.STORAGE.value
    assert failure.fields["violationCodes"] == ["SOURCE_RAW_ID_NOT_IN_TASK"]
    assert failure.fields["violationCount"] == 1
    # rawId 원문은 예외 메시지에만 있고 구조화 필드로는 나가지 않는다.
    assert unknown_raw_id not in str(failure.fields)


def test_timeout_returns_failed(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", slow)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)
    client = _client()

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.submit_calls == []
    assert client.last_callback.error_code == int(ErrorCode.PIPELINE_TIMEOUT)


def _raise(exc: Exception):
    def _fail(*args, **kwargs):
        raise exc

    return _fail
