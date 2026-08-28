"""카탈로그 코드를 갖는 예외 계층과, 실패를 한 번에 기록하는 공통 헬퍼.

두 가지를 제공한다.

1. :class:`AppError` — 자기 :class:`~app.core.error_codes.ErrorCode` 를 아는 예외의
   기반. 도메인 예외는 모두 이걸 상속해서, 잡는 쪽이 예외 클래스명을 알아보지
   않고도 코드를 꺼낼 수 있게 한다.
2. :func:`report_error` — **실패에 정수 코드를 부여하고 로컬 진단으로 남기는 통로**.
   ``except`` 블록이 이것만 호출하면 로그·API 응답·콜백이 같은 실패에 다른 값을
   쓸 수가 없다. 각자 로그를 찍으면 언젠가 갈리는데, 그때 갈렸다는 사실조차 알기 어렵다.

여기서 남는 **진단 줄** 자체는 Elasticsearch 로 가지 않는다(이슈 #53). 수집 대상은
:mod:`app.core.operational_logging` 의 운영 이벤트뿐이고, 표식이 없는 줄은 수집기가 버린다.

다만 :func:`report_error` 는 그 줄을 남긴 뒤 **표식 달린 저하 이벤트를 한 건 더 낸다**
(``app.degraded``, 이슈 #101). 진단 줄과 이벤트는 별개이며, 이벤트에는 emitter 의
allowlist 를 통과한 필드만 실린다 — ``context`` 의 임의 키도, 예외 원문도, traceback 도
거기에는 없다. 이것이 없으면 흡수 경계의 실패가 운영에서 통째로 보이지 않는다.
작업 전체의 성패는 여전히 `timeline.task.completed` 같은 완료 이벤트의 ``errorCode`` 가 답한다.

그래서 이 함수는 다시 던지는 중간 경계가 아니라 **최종 경계나 흡수 지점**에서 부른다.
항목 단위 루프에서 부를 때는 ``emit=False`` 로 이벤트만 뺀다.

원본 예외 메시지(``str(exc)``)와 traceback 은 **로컬 로그에만** 남는다. 외부(API 응답·
콜백)로는 :func:`~app.core.error_codes.message_for` 의 안전 메시지만 나간다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from app.core.error_codes import ErrorCode, message_for
from app.core.logging import log_fields
from app.core.operational_logging import emit_degraded

if TYPE_CHECKING:  # pragma: no cover - 타입 검사 전용
    from app.core.execution_context import ExecutionStage


class AppError(Exception):
    """자기 오류 코드를 아는 예외의 기반.

    ``detail`` 은 **내부 진단용**이다. 어느 규칙이 왜 깨졌고 어느 데이터가 문제인지
    구체적으로 담아도 되며, 로그에만 나간다. 외부로 나가는 문장은 코드에 묶인
    :attr:`message` 다.
    """

    #: 이 예외 종류의 기본 코드. 하위 클래스가 덮어쓴다.
    default_code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR

    def __init__(self, detail: str = "", *, code: ErrorCode | None = None) -> None:
        self.code = code if code is not None else type(self).default_code
        # detail 이 비면 안전 메시지로 채운다. 로그에 빈 줄이 남는 것을 막는 용도라
        # 외부 노출과는 무관하다.
        self.detail = detail or message_for(self.code)
        super().__init__(self.detail)

    @property
    def message(self) -> str:
        """외부(API 응답·콜백)로 내보내도 안전한 메시지."""

        return message_for(self.code)


def code_of(exc: BaseException) -> ErrorCode:
    """예외를 카탈로그 코드로 옮긴다.

    :class:`AppError` 는 자기 코드를 그대로 쓰고, 그 밖에는 아는 만큼만 분류한다.
    분류하지 못한 예외는 :data:`~app.core.error_codes.ErrorCode.INTERNAL_ERROR` 다 —
    모르는 실패를 그럴듯한 코드로 찍어 두면 집계가 조용히 틀어진다.
    """

    if isinstance(exc, AppError):
        return exc.code
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorCode.PIPELINE_TIMEOUT
    return ErrorCode.INTERNAL_ERROR


def code_of_or(exc: BaseException, fallback: ErrorCode) -> ErrorCode:
    """예외가 가진 구체 코드를 보존하고, 미분류 예외에만 단계 코드를 붙인다.

    Agent 같은 흡수 경계는 자기 단계 코드가 필요하지만, 구조화 출력·DB처럼 이미
    원인이 분류된 예외까지 단계 코드로 덮으면 같은 실패가 서로 다른 코드로 보인다.
    """

    code = code_of(exc)
    return fallback if code is ErrorCode.INTERNAL_ERROR else code


def safe_message(exc: BaseException) -> str:
    """예외에 대응하는 **외부 노출용** 메시지를 돌려준다.

    ``str(exc)`` 를 절대 쓰지 않는다. 원본 메시지에는 taskId·rawId·경로·쿼리 같은
    내부 식별자와 구현 세부가 들어 있고, 그대로 내보내면 실패할 때마다 조금씩
    내부 구조가 새어 나간다.
    """

    return message_for(code_of(exc))


def report_error(
    logger: logging.Logger,
    code: ErrorCode,
    summary: str,
    *,
    exc: BaseException | None = None,
    context: dict[str, Any] | None = None,
    stage: "ExecutionStage | None" = None,
    agent: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_version: str | None = None,
    duration_ms: float | None = None,
    level: int = logging.WARNING,
    exc_info: bool = False,
    component: str | None = None,
    emit: bool = True,
) -> ErrorCode:
    """실패 하나에 정수 ``errorCode`` 를 부여하고 로컬 진단 로그로 남긴다.

    ``message`` 는 사람이 읽을 한 줄이고, 기계가 읽을 값은 전부 구조화 필드로 나간다::

        {"message": "타임라인 처리 실패", "errorCode": 1201,
         "errorType": "TimeoutError", "taskId": "abc-1", "stage": "MAIN_AGENT"}

    이 줄에는 수집 표식이 없어 Elasticsearch 로 가지 않는다(이슈 #53). 돌려주는 코드를
    호출부가 응답·콜백·운영 이벤트에 실어야 세 곳이 같은 값을 말한다.
    ``taskId``/``stage``/``agent`` 는 실행 컨텍스트가 자동으로 붙이므로 ``context`` 에
    다시 넣지 않아도 된다.

    Args:
        logger: 호출 지점 모듈의 로거.
        code: 카탈로그 코드.
        summary: 무슨 실패인지 한글 한 줄(식별자·원문 값은 ``context`` 로 넘긴다).
        exc: 원본 예외. 있으면 ``errorType``/``errorMessage`` 를 필드로 붙인다.
        context: 구조화 필드로 붙일 진단 정보(rawId, httpStatus 등).
        stage: 실패한 단계. 생략하면 현재 실행 컨텍스트의 단계를 쓴다.
        agent: agent 라벨. 생략하면 현재 실행 컨텍스트 값을 쓴다.
        provider: LLM provider(LLM 호출 실패에서 쓴다).
        model: LLM model.
        provider_version: provider SDK 버전.
        duration_ms: 실패까지 걸린 시간(ms).
        level: 로그 레벨. 기본 WARNING.
        exc_info: traceback 까지 남길지. 최종 실패에만 True 로 둔다.
        component: 저하 이벤트의 ``component``. 생략하면 ``stage`` 나 실행 컨텍스트에서
            온다. task 밖(기동 등)이라 단계가 없을 때만 명시한다.
        emit: 저하 운영 이벤트를 함께 발행할지. **항목 단위 루프에서만 False 로 둔다** —
            수집 항목마다·사진마다·LLM 호출마다 부르는 자리는 한 작업에서 수십 건이
            되므로, 그런 곳은 빠진 양을 ``droppedCount`` 집계 1건으로 대신 낸다.

    Returns:
        기록한 코드(호출부가 그대로 콜백/응답에 쓸 수 있게 돌려준다).
    """

    fields: dict[str, Any] = {
        "errorCode": int(code),
        "stage": stage.value if stage is not None else None,
        "agent": agent,
        "provider": provider,
        "model": model,
        "providerVersion": provider_version,
        "durationMs": duration_ms,
    }
    if exc is not None:
        fields["errorType"] = type(exc).__name__
        if not exc_info:
            # 원본 메시지는 로그에만 남고 외부(API 응답·콜백)로는 나가지 않는다.
            # traceback 을 남길 때는 포매터가 `error.message` 를 채우므로 생략한다.
            fields["errorMessage"] = str(exc)
    fields.update(context or {})

    # summary 는 인자로 넘긴다. format string 에 직접 넣으면 그 안의 '%' 가 로깅의
    # %-포매팅에 걸려 메시지가 통째로 깨진다.
    logger.log(level, "%s", summary, extra=log_fields(**fields), exc_info=exc_info)

    if emit:
        # 위 진단 줄에는 표식이 없어 Elasticsearch 로 가지 않는다. 같은 실패를 표식 달린
        # 이벤트로 한 건 더 낸다(#101). **여기 실리는 것은 emitter 의 allowlist 를 통과한
        # 필드뿐이다** — `context` 의 임의 키도, `errorMessage`(예외 원문)도, traceback 도
        # 그 줄에는 들어가지 않는다. 원문은 위 진단 줄에만 남는다.
        emit_degraded(
            component or stage,
            error_code=int(code),
            error_type=type(exc).__name__ if exc is not None else None,
            agent=agent,
            duration_ms=duration_ms,
            provider=provider,
            model=model,
            provider_version=provider_version,
            trace_fields=context,
            level=level,
        )

    return code
