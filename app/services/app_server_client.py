"""App Server 서버간 API 클라이언트 (이슈 #40, #64).

AI 서버가 밖으로 나가는 호출은 전부 여기를 지난다. 네 개다.

=================  ==================================================  ================
용도               경로                                                body
=================  ==================================================  ================
입력 조회          ``GET  /timeline/drafts/{taskId}/input``             응답에 sourceItems
결과 저장          ``POST /timeline/drafts/{taskId}/result``            요청에 events
완료 통보          ``POST /timeline/drafts/{taskId}/callback``          요청에 status
메모리 저장        ``POST /user-memory/updates/{taskId}/result``        요청에 status+userMemory
=================  ==================================================  ================

네 호출이 헤더·재시도·상태코드 해석을 공유하므로 한 모듈이 소유한다. 경로마다
따로 짜면 언젠가 한쪽만 고쳐지고, 그 사실을 알아채기 어렵다.

앞의 셋은 타임라인 한 건의 순서 계약(저장 200 → 콜백)을 이룬다. 마지막 하나는
User Memory 갱신(#64)의 **유일한 바깥 호출**이라 순서 계약이 없다 — 성공도 실패도
그 한 번으로 통보한다.

## 토큰

작업 하나에 토큰 하나(:class:`TaskToken`)다. 최초 값은 접수 요청 body 로 들어오고,
타임라인 경로에서는 그 뒤 **응답 body 의 ``taskToken`` 으로 계속 갱신된다**. User
Memory 갱신은 호출이 하나뿐이라 갱신될 기회 자체가 없다. 인증은 언제나 요청 헤더
``Task-Token`` 이다 — 토큰을 URL 이나 요청 body 에 넣지 않는다.

토큰 값은 **어디에도 기록하지 않는다**. 로그·관측 이벤트·Langfuse trace 어디에도
남기지 않으며, :class:`TaskToken` 의 ``repr`` 자체가 마스킹돼 있어 객체를 통째로
찍어도 값이 새지 않는다.

## 실패 분류

App Server 응답 코드를 카탈로그 코드로 옮긴다. 두 갈래로 갈린다.

- **중단(abort)** — 401(토큰 거절)/404(task 없음·만료)/409(순서 충돌). 재시도해도
  같은 답이 오고, 콜백을 보내도 같은 이유로 거절된다. 그래서 콜백 없이 멈춘다.
- **실패(통보 가능)** — 그 밖의 4xx, 그리고 재시도까지 소진한 5xx/timeout. 토큰과
  task 는 살아 있으므로 FAILED 콜백으로 알린다.

재시도는 timeout 과 5xx 에만 한다. 같은 토큰·같은 body 로 다시 보내며, 시도 간
대기는 2배씩 늘린다.

## 운영 이벤트 (이슈 #53)

외부 연동은 AI 서버 장애의 절반이라 Elasticsearch 로 보내는 몇 안 되는 이벤트다.

- ``dependency.request.completed`` — 논리적 호출 하나의 종료. operation, 시도 횟수,
  HTTP 상태, 소요시간, 실패면 errorCode.
- ``dependency.request.retry`` — 재시도 한 번. 사유는 예외 클래스명이나 HTTP 상태다.

URL, 요청·응답 body, 토큰 값, 예외 원문은 어느 이벤트에도 넣지 않는다. 토큰은
값 대신 갱신 횟수(``tokenRefreshCount``)만 종료 이벤트에 실린다.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError, report_error
from app.core.logging import get_logger
from app.core.operational_logging import (
    EventOutcome,
    OperationalEvent,
    emit_event,
)
from app.schemas import CollectedSnapshot, TimelineCallbackPayload, UserMemory
from app.schemas.timeline_input import TimelineInputResponse
from app.schemas.timeline_result import TimelineResultRequest
from app.schemas.user_memory_update import UserMemoryResultRequest
from app.services.source_contract import SourceBatchError, ensure_source_contract

logger = get_logger(__name__)

TASK_TOKEN_HEADER = "Task-Token"
TASK_TOKEN_FIELD = "taskToken"

#: 외부 연동 이벤트의 상대 이름. 상대가 늘면 값이 늘고, 필드 이름은 그대로다.
DEPENDENCY_NAME = "app-server"

INPUT_PATH = "/timeline/drafts/{taskId}/input"
RESULT_PATH = "/timeline/drafts/{taskId}/result"
CALLBACK_PATH = "/timeline/drafts/{taskId}/callback"
USER_MEMORY_RESULT_PATH = "/user-memory/updates/{taskId}/result"

#: 재시도해도 결과가 달라지지 않고, 콜백조차 같은 이유로 거절되는 상태.
_ABORT_STATUSES: dict[int, ErrorCode] = {
    401: ErrorCode.APP_SERVER_UNAUTHORIZED,
    404: ErrorCode.APP_SERVER_TASK_NOT_FOUND,
    409: ErrorCode.APP_SERVER_CONFLICT,
}


@dataclass
class _CallProgress:
    """논리적 호출 하나가 어디까지 갔는지. 종료 이벤트가 이 값을 읽는다.

    성공/실패 어느 쪽으로 끝나든 같은 값을 봐야 해서 반환값이 아니라 호출부가 들고
    있는 상자에 적는다.
    """

    #: 실제로 보낸 시도 횟수(1부터).
    attempts: int = 0
    #: 마지막으로 받은 HTTP 상태. 응답을 받지 못했으면 None(연결 실패·timeout).
    http_status: int | None = None


class AppServerError(AppError):
    """App Server API 호출 실패.

    ``abort`` 가 참이면 완료 콜백까지 포기하고 멈춘다(토큰/task 자체가 무효).
    거짓이면 실패로 처리하되 FAILED 콜백은 보낸다.
    """

    default_code = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        detail: str,
        *,
        code: ErrorCode,
        abort: bool = False,
    ) -> None:
        super().__init__(detail, code=code)
        self.abort = abort


class TaskToken:
    """작업 하나에 붙는 인증 토큰. 응답 body 로 새 값이 오면 갈아 끼운다.

    값을 밖으로 흘리지 않으려고 ``repr``/``str`` 을 마스킹한다. 토큰이 로그에 남는
    사고는 대개 "객체를 통째로 찍었다" 에서 나오므로, 필드를 조심하는 것보다
    타입 자체가 안전한 편이 낫다.
    """

    __slots__ = ("_value", "refresh_count")

    def __init__(self, initial: str) -> None:
        if not initial:
            raise ValueError("taskToken은 비어 있을 수 없습니다.")
        self._value = initial
        #: 갱신 횟수. 값 없이 "몇 번 바뀌었는지" 만 관측에 남긴다.
        self.refresh_count = 0

    @property
    def value(self) -> str:
        """현재 유효한 토큰. 요청 헤더에만 쓴다."""

        return self._value

    def absorb(self, body: Any) -> bool:
        """응답 body 에 새 토큰이 있으면 갱신한다.

        Returns:
            갱신했으면 True. 토큰이 없거나 같은 값이면 False(정상이다 — 계약상
            모든 응답이 토큰을 실어 주지는 않는다).
        """

        if not isinstance(body, dict):
            return False
        candidate = body.get(TASK_TOKEN_FIELD)
        if not isinstance(candidate, str) or not candidate:
            return False
        if candidate == self._value:
            return False
        self._value = candidate
        self.refresh_count += 1
        return True

    def __repr__(self) -> str:  # pragma: no cover - 디버거/로그 방어용
        return f"TaskToken(refreshCount={self.refresh_count}, value=***)"

    __str__ = __repr__


class AppServerClient(ABC):
    """App Server 서버간 API 접근 인터페이스."""

    @abstractmethod
    async def fetch_input(self, task_id: str, token: TaskToken) -> CollectedSnapshot:
        """수집 원본을 조회한다. 실패는 :class:`AppServerError` 로 던진다."""

    @abstractmethod
    async def submit_result(
        self,
        task_id: str,
        token: TaskToken,
        request: TimelineResultRequest,
    ) -> None:
        """확정 결과를 저장한다. 200 을 받아야만 정상 반환한다."""

    @abstractmethod
    async def send_callback(
        self,
        task_id: str,
        token: TaskToken,
        payload: TimelineCallbackPayload,
    ) -> bool:
        """완료 상태를 통보한다. 실패해도 예외 대신 False 를 돌려준다."""

    @abstractmethod
    async def submit_user_memory(
        self,
        task_id: str,
        token: TaskToken,
        request: UserMemoryResultRequest,
    ) -> None:
        """User Memory 갱신 결과를 저장한다(#64).

        **성공과 실패가 같은 경로로 나간다.** 콜백이 없으므로 이 호출이 곧 종료
        통보다. 실패는 :class:`AppServerError` 로 던진다.
        """


class HttpAppServerClient(AppServerClient):
    """httpx 기반 구현."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_sec: float | None = None,
        max_attempts: int | None = None,
        backoff_sec: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.app_server_api_url).rstrip("/")
        self._timeout_sec = timeout_sec or settings.app_server_timeout_sec
        self._max_attempts = max_attempts or settings.app_server_max_attempts
        self._backoff_sec = (
            backoff_sec
            if backoff_sec is not None
            else settings.app_server_retry_backoff_sec
        )
        # 전송 계층 주입 지점. 운영에서는 None(기본 전송)이고, 테스트가
        # `httpx.MockTransport` 를 꽂아 네트워크 없이 응답을 지정한다.
        self._transport = transport

    # --- 공개 API ------------------------------------------------------

    async def fetch_input(self, task_id: str, token: TaskToken) -> CollectedSnapshot:
        body = await self._request(
            "GET",
            self._url(INPUT_PATH, task_id),
            task_id=task_id,
            token=token,
            operation="input",
            # 입력 조회의 404 는 "이 task 의 수집 원본이 없다" 는 뜻이라 전용 코드가
            # 이미 있다(1101). 다른 경로의 404(1405)와 구분해 둔다.
            not_found_code=ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND,
            failure_code=ErrorCode.SOURCE_FETCH_FAILED,
        )

        try:
            parsed = TimelineInputResponse.model_validate(body)
        except ValidationError as exc:
            raise SourceBatchError(
                f"입력 조회 응답이 계약을 어겼습니다: taskId={task_id}, error={exc}"
            ) from exc

        snapshot = parsed.to_snapshot(user_memory=self._user_memory_of(parsed))
        ensure_source_contract(task_id, snapshot)
        return snapshot

    @staticmethod
    def _user_memory_of(parsed: TimelineInputResponse) -> UserMemory | None:
        """``userMemory`` 계약 위반을 흡수한다(#65).

        User Memory 는 해석을 돕는 보조 context 다. 이것 하나가 계약을 어겼다고
        하루치 수집 원본을 버리면 손해가 훨씬 크다. 그래서 코드 1106 으로 기록만
        남기고 값 없이 진행한다 — 결과는 User Memory 가 애초에 없던 날과 같다.

        본문은 어디에도 남기지 않는다. 진단으로 남는 것은 **어떤 필드가 어떤 규칙에
        걸렸는지**(위치와 오류 종류)뿐이다.

        예외 객체를 :func:`report_error` 에 넘기지 않는 것은 실수가 아니다.
        ``str(ValidationError)`` 는 걸린 값을 ``input_value=...`` 로 그대로 인용해서,
        그대로 넘기면 User Memory 본문이 운영 로그에 남는다.
        """

        try:
            return parsed.parse_user_memory()
        except ValidationError as exc:
            errors = exc.errors()
            report_error(
                logger,
                ErrorCode.USER_MEMORY_CONTRACT_VIOLATION,
                "userMemory 계약 위반으로 User Memory 없이 진행합니다",
                context={
                    "taskId": parsed.task_id,
                    "errorType": type(exc).__name__,
                    "userMemoryErrorCount": len(errors),
                    "userMemoryErrorFields": sorted(
                        {
                            ".".join(str(part) for part in error["loc"])
                            for error in errors
                        }
                    ),
                    "userMemoryErrorTypes": sorted(
                        {str(error["type"]) for error in errors}
                    ),
                },
            )
            return None

    async def submit_result(
        self,
        task_id: str,
        token: TaskToken,
        request: TimelineResultRequest,
    ) -> None:
        await self._request(
            "POST",
            self._url(RESULT_PATH, task_id),
            task_id=task_id,
            token=token,
            operation="result",
            not_found_code=ErrorCode.APP_SERVER_TASK_NOT_FOUND,
            failure_code=ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED,
            json_body=request.model_dump(by_alias=True, mode="json"),
        )

    async def send_callback(
        self,
        task_id: str,
        token: TaskToken,
        payload: TimelineCallbackPayload,
    ) -> bool:
        try:
            await self._request(
                "POST",
                self._url(CALLBACK_PATH, task_id),
                task_id=task_id,
                token=token,
                operation="callback",
                not_found_code=ErrorCode.APP_SERVER_TASK_NOT_FOUND,
                failure_code=ErrorCode.CALLBACK_SEND_FAILED,
                json_body=payload.model_dump(by_alias=True, mode="json"),
            )
        except AppServerError:
            # 콜백 실패는 이미 끝난 처리를 되돌리지 않는다. 호출 자체의 실패는
            # `_request` 가 외부 연동 이벤트로 이미 닫았고, "결과를 통보하지 못했다"
            # 는 사실은 호출부(timeline_runner)가 소유한다. 여기서 한 번 더 남기면
            # 같은 실패가 두 곳에서 집계된다.
            return False

        logger.debug("콜백 전송 완료: operation=callback, status=%s", payload.status.value)
        return True

    async def submit_user_memory(
        self,
        task_id: str,
        token: TaskToken,
        request: UserMemoryResultRequest,
    ) -> None:
        await self._request(
            "POST",
            self._url(USER_MEMORY_RESULT_PATH, task_id),
            task_id=task_id,
            token=token,
            operation="user-memory-result",
            not_found_code=ErrorCode.APP_SERVER_TASK_NOT_FOUND,
            failure_code=ErrorCode.USER_MEMORY_SUBMIT_FAILED,
            json_body=request.model_dump(by_alias=True, mode="json"),
        )

    # --- 내부 ----------------------------------------------------------

    def _url(self, path: str, task_id: str) -> str:
        encoded = quote(task_id, safe="")
        return f"{self._base_url}{path.replace('{taskId}', encoded)}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        task_id: str,
        token: TaskToken,
        operation: str,
        not_found_code: ErrorCode,
        failure_code: ErrorCode,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """재시도를 포함한 **논리적 호출 하나**를 수행하고 결과를 이벤트로 닫는다.

        재시도가 몇 번이었든 종료 이벤트는 한 건이다. 운영자가 보는 단위는 "입력
        조회가 됐는가" 이지 "세 번째 시도가 어땠는가" 가 아니다 — 그건 retry
        이벤트가 따로 답한다.

        Returns:
            2xx 응답의 JSON body(없거나 JSON 이 아니면 ``None``).

        Raises:
            AppServerError: 중단 대상(401/404/409)이거나, 재시도까지 소진했거나,
                재시도 대상이 아닌 4xx 를 받은 경우.
        """

        progress = _CallProgress()
        started = perf_counter()
        try:
            body = await self._attempt_with_retries(
                progress,
                method,
                url,
                task_id=task_id,
                token=token,
                operation=operation,
                not_found_code=not_found_code,
                failure_code=failure_code,
                json_body=json_body,
            )
        except AppServerError as exc:
            self._emit_completed(
                operation,
                task_id,
                token,
                progress,
                started,
                outcome=EventOutcome.FAILURE,
                error_code=exc.code,
            )
            raise

        self._emit_completed(
            operation,
            task_id,
            token,
            progress,
            started,
            outcome=EventOutcome.SUCCESS,
        )
        return body

    def _emit_completed(
        self,
        operation: str,
        task_id: str,
        token: TaskToken,
        progress: "_CallProgress",
        started: float,
        *,
        outcome: EventOutcome,
        error_code: ErrorCode | None = None,
    ) -> None:
        emit_event(
            OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
            outcome=outcome,
            level=(
                logging.INFO if outcome is EventOutcome.SUCCESS else logging.ERROR
            ),
            dependency=DEPENDENCY_NAME,
            operation=operation,
            httpStatus=progress.http_status,
            attempts=progress.attempts,
            durationMs=round((perf_counter() - started) * 1000, 3),
            errorCode=int(error_code) if error_code is not None else None,
            taskId=task_id,
            tokenRefreshCount=token.refresh_count,
        )

    async def _attempt_with_retries(
        self,
        progress: "_CallProgress",
        method: str,
        url: str,
        *,
        task_id: str,
        token: TaskToken,
        operation: str,
        not_found_code: ErrorCode,
        failure_code: ErrorCode,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """재시도 정책을 적용해 실제 요청을 보낸다(진행 상황은 ``progress`` 에)."""

        last_detail = ""
        for attempt in range(1, self._max_attempts + 1):
            progress.attempts = attempt
            try:
                response = await self._send(method, url, token, json_body)
            except httpx.HTTPError as exc:
                # 연결 실패·timeout 은 응답 자체가 없으므로 토큰이 바뀌지 않았다.
                # 같은 토큰·같은 body 로 다시 보낸다.
                last_detail = f"transport error: {exc}"
                if attempt >= self._max_attempts:
                    break
                await self._wait_before_retry(
                    attempt, task_id, operation, reason=type(exc).__name__
                )
                continue

            status_code = response.status_code
            progress.http_status = status_code

            if 200 <= status_code < 300:
                body = _json_body(response)
                # 토큰 갱신은 성공 응답에서만 받는다. 실패 응답 body 의 값을 물면
                # 거절된 흐름이 준 토큰으로 다음 요청을 보내게 된다.
                # 갱신 사실은 값 없이 종료 이벤트의 `tokenRefreshCount` 로 나간다.
                token.absorb(body)
                return body

            abort_code = _ABORT_STATUSES.get(status_code)
            if abort_code is not None:
                code = (
                    not_found_code if status_code == 404 else abort_code
                )
                raise AppServerError(
                    f"App Server 가 요청을 거절했습니다: taskId={task_id}, "
                    f"operation={operation}, status={status_code}",
                    code=code,
                    abort=True,
                )

            if status_code < 500:
                # 400 등: 우리가 보낸 것이 잘못됐다. 다시 보내도 같은 답이 온다.
                raise AppServerError(
                    f"App Server 가 요청을 거부했습니다: taskId={task_id}, "
                    f"operation={operation}, status={status_code}",
                    code=failure_code,
                )

            last_detail = f"status={status_code}"
            if attempt >= self._max_attempts:
                break
            await self._wait_before_retry(
                attempt,
                task_id,
                operation,
                reason="server_error",
                http_status=status_code,
            )

        raise AppServerError(
            f"App Server 호출이 재시도까지 실패했습니다: taskId={task_id}, "
            f"operation={operation}, attempts={self._max_attempts}, {last_detail}",
            code=failure_code,
        )

    async def _send(
        self,
        method: str,
        url: str,
        token: TaskToken,
        json_body: dict[str, Any] | None,
    ) -> httpx.Response:
        """요청 하나를 보낸다.

        요청마다 client 를 새로 연다. 이 클라이언트는 프로세스 싱글턴이고 백그라운드
        task 는 요청마다 다른 event loop 에서 돌 수 있어, 연결을 들고 있으면 닫힌
        loop 에 묶인 커넥션을 재사용하게 된다.
        """

        async with httpx.AsyncClient(
            timeout=self._timeout_sec,
            transport=self._transport,
        ) as client:
            return await client.request(
                method,
                url,
                headers={TASK_TOKEN_HEADER: token.value},
                json=json_body,
            )

    async def _wait_before_retry(
        self,
        attempt: int,
        task_id: str,
        operation: str,
        *,
        reason: str,
        http_status: int | None = None,
    ) -> None:
        """다음 시도까지 기다리고, 재시도 사실을 운영 이벤트로 남긴다.

        ``reason`` 은 예외 클래스명(`ConnectTimeout` 등)이나 ``server_error`` 처럼
        고정된 라벨이다. 예외 원문은 URL·body 를 품을 수 있어 싣지 않는다.
        """

        delay = self._backoff_sec * (2 ** (attempt - 1))
        emit_event(
            OperationalEvent.DEPENDENCY_REQUEST_RETRY,
            outcome=EventOutcome.FAILURE,
            level=logging.WARNING,
            dependency=DEPENDENCY_NAME,
            operation=operation,
            attempt=attempt,
            maxAttempts=self._max_attempts,
            reason=reason,
            httpStatus=http_status,
            delayMs=round(delay * 1000, 3),
            taskId=task_id,
        )
        if delay > 0:
            await asyncio.sleep(delay)


def _json_body(response: httpx.Response) -> Any:
    """응답 body 를 JSON 으로 읽는다. 없거나 JSON 이 아니면 ``None``.

    결과 저장 성공 응답은 계약상 body 가 없다. 그걸 오류로 다루면 정상 흐름이
    실패한다.
    """

    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


@lru_cache
def get_app_server_client() -> AppServerClient:
    """App Server 클라이언트 싱글턴을 반환한다(FastAPI 의존성)."""

    return HttpAppServerClient()
