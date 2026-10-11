"""백그라운드 타임라인 처리와 App Server 연동을 검증한다.

이슈 #40 이후 데이터 경로는 App Server API 하나다: 입력 조회 → 추론 → 결과 저장
→ 콜백. 순서와 실패 분류, 토큰 취급이 이 파일의 계약이다.
"""

import asyncio
import logging

from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import report_error
from app.core.execution_context import (
    ExecutionStage,
    record_structured_failure,
)
from app.core.operational_logging import OperationalEvent
from app.core.structured import StructuredOutputError
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TaskStatus,
    TimelineDraft,
    TimelineEventDraft,
)
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
    async def fake_main_agent(request, **_kwargs):
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


def _task_events(caplog) -> list[dict]:
    """백그라운드 작업 완료 운영 이벤트(이슈 #53)."""

    return [
        payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
        and payload.get("event.action")
        == OperationalEvent.TIMELINE_TASK_COMPLETED.value
    ]


def test_success_closes_the_task_with_one_operational_event(monkeypatch, caplog):
    """작업 하나 = 이벤트 하나. 백그라운드 전체 소요시간이 여기 있다."""

    _patch_agent(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        status = _run(_client())

    assert status is TaskStatus.SUCCESS
    events = _task_events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["event.outcome"] == "success"
    assert event["taskId"] == _TASK_ID
    assert event["status"] == TaskStatus.SUCCESS.value
    assert event["callbackSent"] is True
    # 202 접수 응답시간과 달리 여기 durationMs 는 백그라운드 처리 전체다.
    assert event["durationMs"] >= 0
    assert "errorCode" not in event
    assert "failureStage" not in event


def test_agent_stage_details_are_not_collected(monkeypatch, caplog):
    """단계·Agent 추적은 Langfuse 담당이다. 수집 대상이 되면 경계가 무너진다."""

    _patch_agent(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _run(_client())

    collected = [
        payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
    ]
    actions = {payload.get("event.action") for payload in collected}
    assert OperationalEvent.TIMELINE_TASK_COMPLETED.value in actions

    stage_lines = [
        record
        for record in caplog.records
        if record.getMessage().startswith("단계 ")
    ]
    # 단계 경계는 남되(로컬 진단) 수집 표식은 없다.
    assert stage_lines
    assert all(
        not hasattr(record, "operational_event") for record in stage_lines
    )


def test_failure_event_carries_the_code_and_the_broken_stage(monkeypatch, caplog):
    _patch_agent(monkeypatch)
    client = _client(
        submit_error=AppServerError(
            "재시도 소진",
            code=ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED,
        )
    )

    with caplog.at_level(logging.DEBUG):
        status = _run(client)

    assert status is TaskStatus.FAILED
    event = _task_events(caplog)[-1]
    assert event["event.outcome"] == "failure"
    assert event["errorCode"] == int(ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED)
    assert event["failureStage"] == ExecutionStage.STORAGE.value
    assert event["callbackSent"] is True
    assert event["status"] == TaskStatus.FAILED.value


def test_timeout_is_also_closed_by_one_event(monkeypatch, caplog):
    async def slow(request, **_kwargs):
        await asyncio.sleep(5)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", slow)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)

    with caplog.at_level(logging.DEBUG):
        _run(_client())

    events = _task_events(caplog)
    assert len(events) == 1
    assert events[0]["errorCode"] == int(ErrorCode.PIPELINE_TIMEOUT)
    assert events[0]["failureStage"] == ExecutionStage.MAIN_AGENT.value


def test_aborted_task_reports_that_no_callback_was_sent(monkeypatch, caplog):
    _patch_agent(monkeypatch)
    client = _client(
        fetch_error=AppServerError(
            "401",
            code=ErrorCode.APP_SERVER_UNAUTHORIZED,
            abort=True,
        )
    )

    with caplog.at_level(logging.DEBUG):
        _run(client)

    event = _task_events(caplog)[-1]
    assert event["callbackSent"] is False
    assert event["errorCode"] == int(ErrorCode.APP_SERVER_UNAUTHORIZED)


def test_task_event_never_carries_user_content(monkeypatch, caplog):
    """이벤트 필드는 emitter 의 allowlist 가 정한다."""

    _patch_agent(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _run(_client())

    event = _task_events(caplog)[-1]
    assert set(event) <= {
        "event.dataset",
        "event.action",
        "event.outcome",
        "taskId",
        "status",
        "durationMs",
        "callbackSent",
        "errorCode",
        "failureStage",
        "timedOut",
    }


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
    async def boom(request, **_kwargs):
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
    async def boom(request, **_kwargs):
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


def _confirming_agent(confirmed: TimelineDraft, *, then_sleep: float = 5.0):
    """확정본을 하나 발행한 뒤 제한 시간을 넘기는 가짜 main agent."""

    async def agent(request, on_confirm=None, **_kwargs):
        if on_confirm is not None:
            on_confirm(confirmed)
        await asyncio.sleep(then_sleep)
        return _draft()

    return agent


def test_timeout_with_confirmed_draft_stores_it_and_succeeds(monkeypatch):
    """제한 시간이 끝나도 확정본이 있으면 버리지 않고 저장한다(이슈 #76).

    저장까지 갔으므로 SUCCESS 이고, 순서 계약도 정상 경로와 같아야 한다.
    """

    confirmed = _draft()
    monkeypatch.setattr(
        timeline_runner, "run_main_agent", _confirming_agent(confirmed)
    )
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)
    client = _client()

    status = _run(client)

    assert status is TaskStatus.SUCCESS
    assert client.order == ["input", "result", "callback"]
    assert client.last_callback.status is TaskStatus.SUCCESS
    assert client.last_callback.error_code is None


def test_timeout_without_confirmed_draft_still_fails_with_1201(monkeypatch):
    """확정본이 없으면 저장할 것이 없다. 지금과 같이 1201 로 끝난다."""

    async def slow(request, **_kwargs):
        await asyncio.sleep(5)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", slow)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)
    client = _client()

    status = _run(client)

    assert status is TaskStatus.FAILED
    assert client.order == ["input", "callback"]
    assert client.last_callback.error_code == int(ErrorCode.PIPELINE_TIMEOUT)


def test_partial_save_marks_the_event_without_an_error_code(monkeypatch, caplog):
    """부분 저장은 성공이다. errorCode 가 붙으면 지연 감시가 실패로 센다."""

    monkeypatch.setattr(
        timeline_runner, "run_main_agent", _confirming_agent(_draft())
    )
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)

    with caplog.at_level(logging.DEBUG):
        _run(_client())

    event = _task_events(caplog)[-1]
    assert event["status"] == TaskStatus.SUCCESS.value
    assert event["timedOut"] is True
    # emitter 는 None 필드를 아예 싣지 않는다. 없다는 것이 곧 "실패가 아니다" 다.
    assert "errorCode" not in event
    assert "failureStage" not in event


def test_normal_completion_is_not_marked_as_timed_out(monkeypatch, caplog):
    _patch_agent(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _run(_client())

    assert _task_events(caplog)[-1]["timedOut"] is False


def test_stored_draft_is_a_snapshot_taken_at_confirm_time(monkeypatch):
    """취소된 뒤에도 스레드가 draft 를 계속 고친다. 저장본이 그때 바뀌면 안 된다.

    `_confirm` 이 복사본을 발행하므로, 발행 뒤의 변경은 저장 payload 에 닿지 못한다.
    """

    live = _draft()
    live.events.append(
        TimelineEventDraft(
            client_event_id="event-001",
            event_type=EventType.REST,
            title="발행 시점의 제목",
            start_time="2026-06-20T10:00:00+09:00",
            end_time="2026-06-20T10:30:00+09:00",
            confidence=0.5,
            inference_level=InferenceLevel.EVIDENCE_BASED,
            source_refs=[
                SourceRef(
                    source_type=EventSourceType.STAY,
                    raw_id=fixture_raw_id("source-101"),
                )
            ],
        )
    )

    async def agent(request, on_confirm=None, **_kwargs):
        if on_confirm is not None:
            # 실제 `_confirm` 과 같은 방식으로 복사본을 발행한다.
            on_confirm(live.model_copy(deep=True))
        # 취소된 뒤에도 살아 있는 스레드가 하는 일을 흉내 낸다.
        live.events[0].title = "취소 뒤에 바뀐 제목"
        await asyncio.sleep(5)
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", agent)
    monkeypatch.setattr(timeline_runner.settings, "pipeline_timeout_sec", 0.05)
    client = _client()

    assert _run(client) is TaskStatus.SUCCESS
    assert client.last_result.events[0].title == "발행 시점의 제목"


def test_timeout_returns_failed(monkeypatch):
    async def slow(request, **_kwargs):
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


# --- #98 구조화 실패로 비어 버린 결과는 저장하지 않는다 ------------------------


def _event_draft() -> TimelineDraft:
    draft = _draft()
    draft.events = [
        TimelineEventDraft(
            client_event_id="event-001",
            event_type=EventType.REST,
            title="집에서 쉬었어요",
            start_time="2026-06-20T10:00:00+09:00",
            end_time="2026-06-20T10:30:00+09:00",
            confidence=0.5,
            inference_level=InferenceLevel.EVIDENCE_BASED,
            source_refs=[
                SourceRef(
                    source_type=EventSourceType.STAY,
                    raw_id=fixture_raw_id("source-101"),
                )
            ],
        )
    ]
    return draft


def _failing_agent(draft: TimelineDraft):
    """구조화 출력 실패를 기록한 뒤 draft 를 돌려주는 가짜 main agent."""

    async def agent(request, **_kwargs):
        record_structured_failure("location")
        return draft

    return agent


def test_empty_result_from_structured_failure_is_not_stored(monkeypatch):
    """입력이 있었는데 구조화 실패로 event 가 0건이면 저장하지 않는다.

    그대로 보내면 App Server 응답에 따라 1303 이 되고 최초 원인인 1202 가 사라진다.
    """

    monkeypatch.setattr(timeline_runner, "run_main_agent", _failing_agent(_draft()))
    client = _client()

    status = _run(client)

    assert status is TaskStatus.FAILED
    # 저장 호출 자체가 없어야 한다.
    assert client.order == ["input", "callback"]
    assert client.last_callback.error_code == int(ErrorCode.STRUCTURED_OUTPUT_INVALID)


def test_empty_result_without_structured_failure_is_still_stored(monkeypatch):
    """Agent 가 event 로 볼 근거를 못 찾은 것은 판단이지 실패가 아니다."""

    _patch_agent(monkeypatch, _draft())
    client = _client()

    status = _run(client)

    assert status is TaskStatus.SUCCESS
    assert client.order == ["input", "result", "callback"]


def test_structured_failure_with_surviving_events_is_stored(monkeypatch):
    """일부 Agent 가 실패했어도 남은 event 가 있으면 하루 기록을 버리지 않는다."""

    monkeypatch.setattr(
        timeline_runner, "run_main_agent", _failing_agent(_event_draft())
    )
    client = _client()

    status = _run(client)

    assert status is TaskStatus.SUCCESS
    assert client.order == ["input", "result", "callback"]


def test_absorbed_failure_shows_up_next_to_a_successful_task(monkeypatch, caplog):
    """**이 이벤트가 존재하는 이유다** (이슈 #101).

    Agent 하나가 죽어도 흡수되면 작업은 SUCCESS 로 끝나고 완료 이벤트에는
    `errorCode` 조차 없다. 같은 `taskId` 로 저하 이벤트가 함께 나가야 "성공했지만
    무엇을 잃었는지" 가 운영에서 보인다.
    """

    async def failing_question_stage(request, **_kwargs):
        report_error(
            logging.getLogger("app.agents.main.main_agent"),
            ErrorCode.QUESTION_GENERATION_FAILED,
            "Question Agent 실행 실패",
            exc=RuntimeError("boom"),
            stage=ExecutionStage.QUESTION_AGENT,
        )
        return _draft()

    monkeypatch.setattr(timeline_runner, "run_main_agent", failing_question_stage)

    with caplog.at_level(logging.DEBUG):
        status = _run(_client())

    assert status is TaskStatus.SUCCESS

    completed = _task_events(caplog)[0]
    assert completed["event.outcome"] == "success"
    assert "errorCode" not in completed

    degraded = [
        payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
        and payload.get("event.action") == OperationalEvent.APP_DEGRADED.value
    ]
    assert len(degraded) == 1
    assert degraded[0]["event.outcome"] == "failure"
    assert degraded[0]["errorCode"] == int(ErrorCode.QUESTION_GENERATION_FAILED)
    assert degraded[0]["component"] == ExecutionStage.QUESTION_AGENT.value
    # 두 줄을 잇는 것은 taskId 다.
    assert degraded[0]["taskId"] == completed["taskId"] == _TASK_ID
