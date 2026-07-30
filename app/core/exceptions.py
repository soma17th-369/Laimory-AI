"""카탈로그 코드를 갖는 예외 계층과, 실패를 한 번에 기록하는 공통 헬퍼.

두 가지를 제공한다.

1. :class:`AppError` — 자기 :class:`~app.core.error_codes.ErrorCode` 를 아는 예외의
   기반. 도메인 예외는 모두 이걸 상속해서, 잡는 쪽이 예외 클래스명을 알아보지
   않고도 코드를 꺼낼 수 있게 한다.
2. :func:`report_error` — **운영 로그와 관측 이벤트를 같은 코드로 한 번에 남기는
   유일한 통로**. 모든 ``except`` 블록이 이것만 호출하면 로그·관측·API 응답·콜백이
   같은 실패에 다른 값을 쓸 수가 없다. 각자 로그를 찍고 각자 emit 하면 언젠가
   갈리는데, 그때 갈렸다는 사실조차 알기 어렵다.

원본 예외 메시지(``str(exc)``)와 traceback 은 **로그에만** 남는다. 외부(API 응답·
콜백)로는 :func:`~app.core.error_codes.message_for` 의 안전 메시지만 나간다.

관측(observability) 모듈 자신의 실패는 ``emit=False`` 로 호출해야 한다. 관측 기록이
깨진 상황에서 관측으로 그 사실을 알리려 하면 같은 실패 경로를 다시 타서 재귀한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from app.core.error_codes import ErrorCode, message_for

if TYPE_CHECKING:  # pragma: no cover - 타입 검사 전용
    from app.core.observability.models import ObservationStage


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
    stage: "ObservationStage | None" = None,
    agent: str | None = None,
    payload: dict[str, Any] | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_version: str | None = None,
    duration_ms: float | None = None,
    emit: bool = True,
    level: int = logging.WARNING,
    exc_info: bool = False,
) -> ErrorCode:
    """실패 하나를 운영 로그와 관측 이벤트에 **같은 코드로** 기록한다.

    로그 줄은 항상 ``errorCode`` 로 시작하는 고정 순서를 갖는다::

        타임라인 처리 실패: errorCode=1201, errorType=TimeoutError, taskId=abc-1

    CloudWatch Logs Insights 에서 ``errorCode`` 로 필터·집계하려면 필드 위치와
    이름이 흔들리지 않아야 해서 순서를 고정했다.

    Args:
        logger: 호출 지점 모듈의 로거.
        code: 카탈로그 코드.
        summary: 무슨 실패인지 한글 한 줄(식별자·원문 값은 ``context`` 로 넘긴다).
        exc: 원본 예외. 있으면 ``errorType``/``error`` 를 로그에 붙인다.
        context: 로그에 ``키=값`` 으로 붙일 진단 정보(taskId, rawId 등).
        stage: 관측 이벤트 단계. 생략하면 현재 컨텍스트 단계를 쓴다.
        agent: 관측 이벤트 agent 라벨.
        payload: 관측 payload 에 더할 항목. ``errorCode``/``errorType`` 은 여기서
            자동으로 채우므로 넣지 않아도 된다.
        provider: 관측 이벤트 provider(LLM 호출 실패에서 쓴다).
        model: 관측 이벤트 model.
        provider_version: 관측 이벤트 providerVersion.
        duration_ms: 실패까지 걸린 시간(ms).
        emit: 관측 이벤트를 낼지 여부. **관측 모듈 자신의 실패는 반드시 False** —
            아니면 같은 실패 경로를 다시 타서 재귀한다.
        level: 로그 레벨. 기본 WARNING.
        exc_info: traceback 까지 남길지. 최종 실패에만 True 로 둔다.

    Returns:
        기록한 코드(호출부가 그대로 콜백/응답에 쓸 수 있게 돌려준다).
    """

    fields: list[str] = ["errorCode=%s"]
    # summary 도 인자로 넘긴다. format string 에 직접 넣으면 그 안의 '%' 가
    # 로깅의 %-포매팅에 걸려 메시지가 통째로 깨진다.
    args: list[Any] = [summary, int(code)]

    if exc is not None:
        fields.append("errorType=%s")
        args.append(type(exc).__name__)

    for key, value in (context or {}).items():
        fields.append(f"{key}=%s")
        args.append(value)

    if exc is not None:
        # 원본 메시지는 마지막에 둔다. 길이가 들쭉날쭉해 앞에 오면 고정 필드를
        # 읽기 어려워진다. 이 값은 로그에만 남고 외부로 나가지 않는다.
        fields.append("error=%s")
        args.append(exc)

    logger.log(level, "%s: " + ", ".join(fields), *args, exc_info=exc_info)

    if emit:
        _emit_failure(
            code,
            exc=exc,
            stage=stage,
            agent=agent,
            payload=payload,
            provider=provider,
            model=model,
            provider_version=provider_version,
            duration_ms=duration_ms,
        )

    return code


def _emit_failure(
    code: ErrorCode,
    *,
    exc: BaseException | None,
    stage: "ObservationStage | None",
    agent: str | None,
    payload: dict[str, Any] | None,
    provider: str | None = None,
    model: str | None = None,
    provider_version: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """관측 FAILED 이벤트를 낸다(관측 컨텍스트가 없으면 조용히 건너뛴다).

    ``app.core.observability`` 를 함수 안에서 import 한다. 관측 모듈들도 실패를
    남기려고 이 모듈을 import 하므로, 최상단에서 서로를 import 하면 순환이 된다.
    """

    from app.core.observability import ObservationEventType, emit_observation

    event_payload: dict[str, Any] = {
        key: value
        for key, value in (payload or {}).items()
        if key not in {"errorCode", "errorType"}
    }
    # 호출부 payload 가 실수로 공통 식별 필드를 덮을 수 없게 마지막에 확정한다.
    event_payload["errorCode"] = int(code)
    if exc is not None:
        event_payload["errorType"] = type(exc).__name__

    emit_observation(
        ObservationEventType.FAILED,
        stage=stage,
        agent=agent,
        payload=event_payload,
        provider=provider,
        model=model,
        provider_version=provider_version,
        duration_ms=duration_ms,
    )
