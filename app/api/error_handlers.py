"""전역 예외 처리기.

이 서버가 HTTP 로 내보내는 실패는 **경로와 무관하게 한 가지 형태**여야 한다.

.. code-block:: json

    {"errorCode": 1001, "error": "요청 형식이 올바르지 않습니다."}

FastAPI 기본값은 그렇지 않다. 요청 검증 실패는 ``{"detail": [...]}`` 로 필드 경로와
입력값을 그대로 돌려주고, 미처리 예외는 본문 없는 500 이 나간다. 클라이언트가
경로마다 다른 모양을 분기해야 하고, 검증 오류 본문에는 우리가 내보낼 생각이 없던
입력값이 그대로 실린다.

여기서 네 가지를 모두 :class:`~app.schemas.error.ErrorResponse` 로 바꾼다.

===============================  ==============================================
``RequestValidationError``        1001 (422). 어느 필드가 왜 틀렸는지는 로그에만.
``AppError``                      예외가 가진 코드. HTTP 상태는 카탈로그 매핑.
``StarletteHTTPException``        상태 코드 → 카탈로그 코드(404/405/그 밖 4xx…).
그 밖의 모든 예외                  1901 (500). traceback 은 로그에만.
===============================  ==============================================

## 로그 (이슈 #53)

처리기는 **자기 이벤트를 만들지 않는다**. 코드를 확정한 뒤
:func:`~app.api.request_logging.annotate_request_error` 로 요청 scope 에 적어 두면,
요청 미들웨어가 method/route/httpStatus/durationMs 와 합쳐 `http.request.completed`
한 건으로 닫는다. 실패한 요청이 두 줄로 갈리던 문제(레벨도 서로 달랐다)가 여기서 사라진다.

:func:`~app.core.exceptions.report_error` 호출은 남지만 이제 **로컬 진단**이다.
표식이 없어 Elasticsearch 로 가지 않으며, 검증 오류는 입력값 대신 위반 개수와
필드 경로만 남긴다.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.request_logging import annotate_request_error
from app.core.error_codes import ErrorCode, code_for_http_status, http_status_for
from app.core.exceptions import AppError, report_error
from app.core.logging import get_logger
from app.schemas.error import ErrorResponse

logger = get_logger(__name__)

#: 로컬 진단에 남길 검증 위반 개수 상한과 항목 길이 상한. 위반 목록은 보낸 쪽이
#: 만든 요청에서 나오므로 우리가 크기를 정한다.
_MAX_VIOLATIONS = 10
_MAX_VIOLATION_LENGTH = 120

#: OpenAPI ``responses`` 에 붙일 공통 오류 응답 스펙. 라우터가 이걸 참조해
#: 명세와 실제 응답이 갈리지 않게 한다.
ERROR_RESPONSES: dict[int | str, dict] = {
    422: {
        "model": ErrorResponse,
        "description": "요청 형식이 올바르지 않습니다. (errorCode 1001)",
    },
    500: {
        "model": ErrorResponse,
        "description": "서버 내부 오류입니다. (errorCode 1901)",
    },
}


def _http_log_level(status_code: int) -> int:
    """로컬 진단 레벨. 4xx 는 클라이언트 잘못이라 INFO, 5xx 는 ERROR.

    Elasticsearch 로 나가는 요청 이벤트의 레벨은 여기서 정하지 않는다
    (:func:`~app.core.operational_logging.http_level` 이 소유한다).
    """

    return logging.INFO if status_code < 500 else logging.ERROR


def _validation_diagnostics(exc: RequestValidationError) -> dict[str, Any]:
    """검증 실패를 **입력값 없이** 요약한다.

    pydantic 의 오류 항목에는 ``input``/``ctx``/``msg`` 로 잘못 보낸 값 자체가 들어
    있다. 여기서는 위반 개수와 "어느 필드가 어떤 규칙을 어겼는지" 만 뽑는다.
    """

    errors = exc.errors()
    violations = [
        (
            f"{'.'.join(str(part) for part in error.get('loc', ()))}"
            f":{error.get('type', 'invalid')}"
        )[:_MAX_VIOLATION_LENGTH]
        for error in errors[:_MAX_VIOLATIONS]
    ]
    return {"violationCount": len(errors), "violationFields": violations}


def _json_error(code: ErrorCode, status_code: int | None = None) -> JSONResponse:
    """카탈로그 코드를 공통 오류 응답으로 직렬화한다."""

    body = ErrorResponse.of(code)
    return JSONResponse(
        status_code=status_code if status_code is not None else http_status_for(code),
        content=body.model_dump(by_alias=True, mode="json"),
    )


async def handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """요청 본문/파라미터가 계약을 어겼다.

    FastAPI 기본 응답은 위반 필드 경로와 **입력값 자체**를 담는다. 그대로 두면
    잘못 보낸 값이 응답으로 되돌아 나가므로, 진단 정보는 로그에만 남기고 응답에는
    코드와 고정 메시지만 내보낸다.

    예외 객체는 로그로 넘기지 않는다 — ``str(exc)`` 에 잘못 보낸 값이 그대로 들어 있다.
    """

    annotate_request_error(
        request,
        ErrorCode.REQUEST_VALIDATION_FAILED,
        error_type=type(exc).__name__,
    )
    report_error(
        logger,
        ErrorCode.REQUEST_VALIDATION_FAILED,
        "요청 검증 실패",
        context=_validation_diagnostics(exc),
        level=logging.INFO,
    )
    return _json_error(ErrorCode.REQUEST_VALIDATION_FAILED)


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """도메인 예외가 요청 처리 중에 올라왔다.

    예외가 자기 코드를 갖고 있으므로 그대로 쓴다. ``exc.detail`` 은 내부 진단용이라
    로그에만 남고, 응답에는 코드에 묶인 안전 메시지가 나간다.
    """

    annotate_request_error(request, exc.code, error_type=type(exc).__name__)
    report_error(logger, exc.code, "요청 처리 실패", exc=exc)
    return _json_error(exc.code)


async def handle_http_exception(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """``HTTPException`` 과 라우팅 실패(404/405)를 공통 계약으로 바꾼다.

    ``exc.detail`` 은 FastAPI 가 채운 영문 문구("Not Found" 등)라 응답에 쓰지 않고,
    상태 코드에서 카탈로그 코드를 끌어와 우리 메시지를 내보낸다.
    """

    code = code_for_http_status(exc.status_code)
    annotate_request_error(request, code, error_type=type(exc).__name__)
    report_error(
        logger,
        code,
        "HTTP 오류 응답",
        exc=exc,
        context={"httpStatus": exc.status_code},
        level=_http_log_level(exc.status_code),
    )
    # 상태 코드는 원래 값을 유지한다. 401/403 등 카탈로그가 4xx 로 뭉뚱그린
    # 코드까지 400 으로 바꿔 버리면 클라이언트가 원인을 구분하지 못한다.
    return _json_error(code, status_code=exc.status_code)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """분류되지 않은 예외 — 마지막 방어선.

    여기까지 온 예외는 우리가 예상하지 못한 것이다. traceback 은 **로컬 진단**으로만
    남기고(표식이 없어 Elasticsearch 로 가지 않는다), 밖으로는 1901 과 고정 메시지만
    내보낸다.

    이 처리기는 Starlette 의 ``ServerErrorMiddleware`` 에서 돌아 요청 로그 미들웨어보다
    바깥이다. 그래서 여기 주석은 요청 이벤트에 닿지 못하고, 미들웨어가 같은 코드로
    이벤트를 이미 닫는다. 주석은 처리기 순서가 바뀌었을 때를 위한 것이다.
    """

    annotate_request_error(
        request,
        ErrorCode.INTERNAL_ERROR,
        error_type=type(exc).__name__,
    )
    report_error(
        logger,
        ErrorCode.INTERNAL_ERROR,
        "미처리 예외",
        exc=exc,
        level=logging.ERROR,
        exc_info=True,
    )
    return _json_error(ErrorCode.INTERNAL_ERROR)


def register_error_handlers(app: FastAPI) -> None:
    """앱에 전역 예외 처리기를 붙인다.

    Starlette 는 발생한 예외의 MRO 를 따라가며 처음 만나는 처리기를 쓴다. 그래서
    ``AppError`` 를 상속한 도메인 예외는 등록 순서와 무관하게 항상
    :func:`handle_app_error` 로 간다 — MRO 상 ``AppError`` 가 ``Exception`` 보다
    앞이라, 코드를 가진 예외가 미분류 방어선으로 흘러가 1901 로 뭉개지지 않는다.
    """

    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
