"""요청 한 건을 운영 이벤트 한 건으로 닫는 ASGI 미들웨어.

uvicorn 의 access log 를 대신한다(`align_uvicorn_loggers` 가 그쪽을 끈다). uvicorn 이
내는 줄은 JSON 이 아니라 Filebeat 가 이벤트를 통째로 버리고, 상태 코드 말고는 필드가
없어 Elasticsearch 에서 걸러 볼 수도 없다.

BaseHTTPMiddleware 가 아니라 순수 ASGI 미들웨어다. `POST /v1/timeline` 은 202 를
돌려준 뒤 BackgroundTasks 로 몇 분짜리 처리를 이어가는데, ASGI 앱 호출이 끝나는
시점을 재면 그 시간까지 요청 지연으로 잡힌다. 그래서 응답 헤더가 나가는 순간
(`http.response.start`)에 기록한다. 백그라운드 처리의 전체 소요시간은
`timeline.task.completed` 이벤트가 따로 답한다.

## 요청당 한 건 (이슈 #53)

실패한 요청도 이벤트는 하나다. 오류 처리기는 자기 로그를 남기지 않고
:func:`annotate_request_error` 로 안전한 ``errorCode``/예외 클래스명만 scope 에
적어 두며, 여기서 method/route/httpStatus/durationMs 와 합쳐 한 번 발행한다.

미분류 예외는 예외다 — Starlette 는 ``Exception`` 처리기를 이 미들웨어보다 **바깥**
(`ServerErrorMiddleware`)에서 돌리므로 주석이 도착하지 못한다. 응답을 시작하지도
못한 그 경우는 여기서 500/`INTERNAL_ERROR` 로 직접 닫는다.

상관값은 `request.state` 가 아니라 전용 scope 키에 담는다. `state` 는 서버/lifespan
쪽이 소유하는 자리라 요청마다 무엇이 들어 있는지가 우리 계약이 아니다.

경로는 라우트 템플릿(`/v1/timeline`)으로 남긴다. 실제 path 를 그대로 쓰면 경로
파라미터가 값으로 실리고, 매칭되지 않은 요청은 보낸 쪽이 만든 문자열이 된다.
쿼리 문자열은 어느 경우에도 남기지 않는다.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Awaitable, Callable

from starlette.requests import Request

from app.core.error_codes import ErrorCode
from app.core.exceptions import stack_trace_of
from app.core.operational_logging import (
    OperationalEvent,
    emit_event,
    http_level,
    http_outcome,
)
from app.core.redaction import redact_text

#: 배포 스크립트와 컨테이너 헬스체크가 수 초마다 두드리는 경로. 정상 응답은
#: 적재하지 않는다 — 남기면 운영 로그가 헬스체크로만 찬다. 실패(4xx/5xx)는 남긴다.
_QUIET_PATHS = frozenset({"/ping", "/health"})

#: 오류 처리기·엔드포인트가 요청 이벤트에 실을 값을 적어 두는 자리.
_ANNOTATION_KEY = "laimory.request.annotation"

#: 매칭되지 않은 요청의 path 상한. 보낸 쪽이 만든 문자열이라 길이를 우리가 정한다.
_MAX_PATH_LENGTH = 120

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def annotate_request_error(
    request: Request,
    code: ErrorCode,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
    error_stack_trace: str | None = None,
) -> None:
    """요청 이벤트에 실을 오류 정보를 적어 둔다(응답 생성 **전**에 부른다).

    예외 원문과 traceback 도 받는다(#109 범위 확장). **검증 실패는 넘기지 않는다** —
    그 예외의 문자열에는 사용자가 보낸 입력값이 그대로 들어 있어, 응답에서 막아 둔 값이
    로그로 되돌아 나간다. 무엇을 넘길지는 처리기가 정하고 여기서는 받은 것만 적는다.
    """

    annotation = _annotation(request.scope)
    if annotation is None:
        return
    annotation["errorCode"] = int(code)
    if error_type is not None:
        annotation["errorType"] = error_type
    if error_message is not None:
        annotation["errorMessage"] = error_message
    if error_stack_trace is not None:
        annotation["errorStackTrace"] = error_stack_trace


def annotate_request_task(request: Request, task_id: str) -> None:
    """접수한 요청의 taskId 를 적어 둔다.

    HTTP 접수 이벤트와 백그라운드 완료 이벤트를 같은 상관키로 잇는다. 검증을
    통과한 값만 넘어온다.
    """

    annotation = _annotation(request.scope)
    if annotation is not None:
        annotation["taskId"] = task_id


def _annotation(scope: Scope) -> dict[str, Any] | None:
    value = scope.get(_ANNOTATION_KEY)
    return value if isinstance(value, dict) else None


def _route(scope: Scope) -> str:
    """라우트 템플릿. 매칭되지 않았으면 길이를 자른 안전한 path."""

    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    path = scope.get("path", "")
    return redact_text(path)[:_MAX_PATH_LENGTH]


class RequestLoggingMiddleware:
    """HTTP 요청의 method·route·상태·소요시간을 운영 이벤트로 남긴다."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        scope[_ANNOTATION_KEY] = {}
        started = perf_counter()
        logged = False

        def emit(
            status_code: int,
            *,
            fallback_error: ErrorCode | None = None,
            exc: BaseException | None = None,
        ) -> None:
            path = scope.get("path", "")
            if status_code < 400 and path in _QUIET_PATHS:
                return
            annotation = _annotation(scope) or {}
            error_code = annotation.get("errorCode")
            if error_code is None and fallback_error is not None:
                error_code = int(fallback_error)
            error_type = annotation.get("errorType")
            error_message = annotation.get("errorMessage")
            stack_trace = annotation.get("errorStackTrace")
            if exc is not None:
                # 미처리 예외는 처리기의 주석이 도착하지 못한다(아래 except 참고).
                # 이 자리가 그 실패의 원인을 남길 유일한 기회다(#109 범위 확장).
                error_type = error_type or type(exc).__name__
                error_message = error_message or str(exc)
                stack_trace = stack_trace or stack_trace_of(exc)
            emit_event(
                OperationalEvent.HTTP_REQUEST_COMPLETED,
                outcome=http_outcome(status_code),
                level=http_level(status_code),
                method=method,
                route=_route(scope),
                httpStatus=status_code,
                durationMs=round((perf_counter() - started) * 1000, 3),
                errorCode=error_code,
                errorType=error_type,
                errorMessage=error_message,
                errorStackTrace=stack_trace,
                taskId=annotation.get("taskId"),
            )

        async def send_wrapper(message: Message) -> None:
            nonlocal logged
            if message["type"] == "http.response.start" and not logged:
                logged = True
                emit(int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # 여기까지 올라온 예외는 응답을 시작하지도 못했다는 뜻이다. 500 응답은
            # 이 미들웨어 **바깥**의 ServerErrorMiddleware 가 만들므로, 그쪽 처리기의
            # 주석을 기다릴 수 없다. 코드를 여기서 확정해 요청 줄을 닫는다.
            if not logged:
                logged = True
                emit(500, fallback_error=ErrorCode.INTERNAL_ERROR, exc=exc)
            raise
