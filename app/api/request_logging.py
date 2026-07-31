"""요청 한 건을 구조화 로그 한 줄로 남기는 ASGI 미들웨어.

uvicorn 의 access log 를 대신한다(`align_uvicorn_loggers` 가 그쪽을 끈다). uvicorn 이
내는 줄은 JSON 이 아니라 Filebeat 가 이벤트를 통째로 버리고, 상태 코드 말고는 필드가
없어 Elasticsearch 에서 걸러 볼 수도 없다.

BaseHTTPMiddleware 가 아니라 순수 ASGI 미들웨어다. `POST /v1/timeline` 은 202 를
돌려준 뒤 BackgroundTasks 로 몇 분짜리 처리를 이어가는데, ASGI 앱 호출이 끝나는
시점을 재면 그 시간까지 요청 지연으로 잡힌다. 그래서 응답 헤더가 나가는 순간
(`http.response.start`)에 기록한다.

쿼리 문자열은 남기지 않는다. 경로와 달리 값이 실릴 수 있는 자리다.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger, log_fields

logger = get_logger(__name__)

#: 배포 스크립트와 컨테이너 헬스체크가 수 초마다 두드리는 경로. INFO 로 남기면
#: 운영 로그가 헬스체크로만 채워진다.
_QUIET_PATHS = frozenset({"/ping", "/health"})

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def _log_level(path: str, status_code: int) -> int:
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    if path in _QUIET_PATHS:
        return logging.DEBUG
    return logging.INFO


class RequestLoggingMiddleware:
    """HTTP 요청의 method·path·상태·소요시간을 구조화 로그로 남긴다."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        started = perf_counter()
        logged = False

        def emit(status_code: int) -> None:
            logger.log(
                _log_level(path, status_code),
                "요청 처리 완료",
                extra=log_fields(
                    method=method,
                    path=path,
                    httpStatus=status_code,
                    durationMs=round((perf_counter() - started) * 1000, 3),
                ),
            )

        async def send_wrapper(message: Message) -> None:
            nonlocal logged
            if message["type"] == "http.response.start" and not logged:
                logged = True
                emit(int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 예외 자체는 전역 예외 처리기가 errorCode 와 함께 이미 남겼다. 응답을
            # 시작하지도 못한 경우에만 요청 줄을 닫아 준다.
            if not logged:
                logged = True
                emit(500)
            raise
