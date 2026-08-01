"""로그 출력 설정.

로컬에서는 rich 핸들러로, 운영에서는 **한 줄 JSON** 으로 초기화한다. 포맷은
`settings.log_format`("rich" | "json") 으로 고른다.

운영(EC2)에서는 컨테이너 stdout 을 별도 Filebeat 컨테이너가 읽는다(이슈 #47).
그래서 로그 한 줄은 **반드시 유효한 JSON 하나**여야 한다. 예외도
`error.stack_trace` 필드에 담아 한 줄 안에 넣는다 — 여러 줄로 흘리면 Filebeat 가
줄마다 다른 이벤트로 쪼개고, multiline 규칙을 맞춰도 스택 모양이 바뀌면 다시 깨진다.

## 두 종류의 줄 (이슈 #53)

- **운영 이벤트** — :mod:`app.core.operational_logging` 의 emitter 만 만든다.
  ``event.dataset``/``event.action``/``event.outcome`` 표식을 달고 나가며,
  필드는 emitter 가 이벤트별 allowlist 로 조립한 것뿐이다. Filebeat 는 이 표식이
  있는 줄만 Elasticsearch 로 보낸다.
- **일반 로그** — 그 밖의 모든 줄. 로컬·컨테이너 진단용이다. 표식이 없으므로
  수집되지 않는다. 여기에 무엇을 남기든 Elasticsearch 수집 범위는 넓어지지 않는다.

일반 로그 한 줄에 들어가는 필드는 세 층이다.

1. 고정: ``timestamp``, ``log.level``, ``logger``, ``message``, ``service``,
   ``environment``, ``version``
2. 실행 컨텍스트(:mod:`app.core.execution_context`): ``taskId``, ``stage``, ``agent``,
   ``iteration``. 호출부가 넘기지 않아도 자동으로 붙는다.
3. 호출부 지정: ``extra=log_fields(errorCode=..., durationMs=...)``

운영 이벤트에는 위 2·3 층을 자동으로 붙이지 않는다. 표식이 달린 줄의 필드는
emitter 가 통제하는 것이 계약이다.

AI 실행 본문(프롬프트·LLM 응답·draft·사용자 원문)은 어느 쪽에도 남기지 않는다.
그 수준의 관측은 Langfuse 가 담당한다.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Iterator

from rich.logging import RichHandler

from app.core.config import settings
from app.core.execution_context import (
    ExecutionStage,
    execution_log_fields,
    execution_scope,
)
from app.core.redaction import redact_text, redact_value

#: 로그·trace 양쪽에서 이 서비스를 가리키는 이름. Langfuse 의 OTel `service.name` 과
#: 같은 값을 써야 두 시스템에서 같은 서비스로 보인다.
SERVICE_NAME = "laimory-ai"

#: 운영 이벤트 payload 를 실어 나르는 LogRecord 속성 이름. emitter 만 채운다.
#: (:mod:`app.core.operational_logging` 이 이 이름으로 ``extra`` 를 넘긴다.)
OPERATIONAL_ATTR = "operational_event"

#: 호출부 필드가 덮어쓸 수 없는 고정 필드. 이 이름들이 흔들리면 Elasticsearch 매핑과
#: 저장된 검색이 통째로 깨진다. 수집 표식(``event.*``)도 여기 있다 — 일반 로그가
#: ``log_fields(**{"event.dataset": ...})`` 같은 식으로 표식을 위조해 수집 대상이
#: 되는 길을 막는다.
_RESERVED_FIELDS = frozenset(
    {
        "timestamp",
        "log.level",
        "logger",
        "message",
        "service",
        "environment",
        "version",
        "error.type",
        "error.message",
        "error.stack_trace",
        "event.dataset",
        "event.action",
        "event.outcome",
    }
)

# 로깅이 중복 초기화되지 않도록 하는 플래그
_configured = False


def log_fields(**fields: Any) -> dict[str, dict[str, Any]]:
    """``logger`` 호출의 ``extra=`` 에 넘길 구조화 필드를 만든다.

    값이 ``None`` 인 항목은 넣지 않는다. 줄마다 ``null`` 을 채우면 Elasticsearch
    매핑만 늘고 검색에는 도움이 되지 않는다.

    사용 예::

        logger.info("결과 저장 완료", extra=log_fields(durationMs=812, events=7))
    """

    return {"fields": {key: value for key, value in fields.items() if value is not None}}


def _iso_timestamp(created: float) -> str:
    return (
        datetime.fromtimestamp(created, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """``extra=log_fields(...)`` 로 넘어온 값을 마스킹해 돌려준다."""

    fields = getattr(record, "fields", None)
    if not isinstance(fields, Mapping):
        return {}
    # dict 통째로 넘긴다. 값만 훑으면 `taskToken` 처럼 **키 이름으로** 걸러야 하는
    # 값이 그대로 나간다.
    safe = redact_value({key: value for key, value in fields.items()})
    return {key: value for key, value in safe.items() if key not in _RESERVED_FIELDS}


def _operational_fields(record: logging.LogRecord) -> dict[str, Any] | None:
    """emitter 가 조립한 운영 이벤트 payload. 일반 로그면 ``None``."""

    payload = getattr(record, OPERATIONAL_ATTR, None)
    if not isinstance(payload, Mapping):
        return None
    # emitter 가 이미 allowlist 로 걸렀다. 마스킹은 마지막 방어선으로 한 번 더 건다.
    return dict(redact_value(dict(payload)))


class JsonLogFormatter(logging.Formatter):
    """로그 레코드를 Filebeat 가 그대로 읽을 수 있는 한 줄 JSON 으로 직렬화한다."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # `@timestamp` 가 아니라 `timestamp` 다. Beats 는 `@timestamp` 를 이벤트
            # 자체의 시각으로 예약해 두므로, 그 이름으로 문자열을 실어 보내면 충돌한다.
            # Filebeat 의 timestamp processor 가 이 값을 읽어 `@timestamp` 로 옮긴다.
            "timestamp": _iso_timestamp(record.created),
            "log.level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "service": SERVICE_NAME,
            "environment": settings.app_env,
            "version": settings.agent_version,
        }
        operational = _operational_fields(record)
        if operational is not None:
            # 운영 이벤트는 emitter 가 조립한 필드가 전부다. 주변 컨텍스트도,
            # 호출부 임의 필드도, 예외 원문·traceback 도 붙이지 않는다(이슈 #53).
            payload.update(operational)
            return json.dumps(payload, ensure_ascii=False, default=str)

        # 컨텍스트 → 호출부 순서로 덮어쓴다. 호출부가 명시한 taskId 가 주변 컨텍스트를
        # 이기는 게 맞다(다른 task 를 대신 기록하는 경로가 있다).
        payload.update(execution_log_fields())
        payload.update(_record_fields(record))

        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, _ = record.exc_info
            payload["error.type"] = exc_type.__name__
            payload["error.message"] = redact_text(str(exc_value))
            payload["error.stack_trace"] = redact_text(
                self.formatException(record.exc_info)
            )
        if record.stack_info:
            payload["error.stack_trace"] = redact_text(
                self.formatStack(record.stack_info)
            )

        return json.dumps(payload, ensure_ascii=False, default=str)


class _RichLogFormatter(logging.Formatter):
    """로컬 콘솔용. 구조화 필드를 사람이 읽을 수 있게 메시지 뒤에 붙인다."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        operational = _operational_fields(record)
        fields = (
            operational
            if operational is not None
            else {**execution_log_fields(), **_record_fields(record)}
        )
        if not fields:
            return message
        rendered = ", ".join(f"{key}={value}" for key, value in fields.items())
        return f"{message} [{rendered}]"


def configure_logging(level: str | None = None) -> None:
    """루트 로거를 초기화한다(포맷은 `settings.log_format`).

    이미 초기화된 경우 아무 작업도 하지 않는다.

    Args:
        level: 로그 레벨. 지정하지 않으면 설정값(`settings.log_level`)을 사용한다.
    """

    global _configured
    if _configured:
        return

    log_level = (level or settings.log_level).upper()

    if settings.log_format.lower() == "json":
        # 로그 본문이 한글이라 stdout 인코딩이 UTF-8 이 아니면 Filebeat 가 읽기 전에
        # 이미 깨진다. 컨테이너 기본값도 UTF-8 이지만 로케일에 맡기지 않는다.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover - 표준 stdout 이 아닌 환경
            pass
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter())
        logging.basicConfig(level=log_level, handlers=[handler])
    else:
        rich_handler = RichHandler(rich_tracebacks=True, show_path=False)
        rich_handler.setFormatter(_RichLogFormatter())
        logging.basicConfig(
            level=log_level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[rich_handler],
        )

    _configured = True


def align_uvicorn_loggers() -> None:
    """uvicorn 로거를 애플리케이션 로그 설정에 합류시킨다.

    uvicorn 은 기동하면서 자기 로거(`uvicorn`, `uvicorn.error`, `uvicorn.access`)에
    직접 핸들러를 붙이고 ``propagate`` 를 끈다. 그대로 두면 같은 stdout 에 JSON 이
    아닌 줄이 섞여 Filebeat 가 이벤트 하나를 통째로 버린다.

    ``uvicorn.access`` 는 끈다. 요청 로그는 :mod:`app.api.request_logging` 미들웨어가
    taskId·durationMs 까지 붙여 남기므로, 그대로 두면 같은 요청이 두 줄이 된다.

    uvicorn 이 로깅을 설정한 **뒤에** 불러야 하므로 앱 lifespan 에서 호출한다.
    """

    configure_logging()

    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = True
    # 끄지 않고 레벨만 올린다. 완전히 비활성화하면 uvicorn 이 access 경로에서 내는
    # 경고까지 사라진다.
    access_logger.setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """이름이 지정된 로거를 반환한다.

    최초 호출 시 로깅 설정이 초기화된다.

    Args:
        name: 보통 `__name__` 을 넘긴다.
    """

    configure_logging()
    return logging.getLogger(name)


@contextmanager
def stage_span(
    logger: logging.Logger,
    stage: ExecutionStage,
    *,
    agent: str | None = None,
    iteration: int | None = None,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """단계 경계를 남기고 그 동안 실행 컨텍스트를 좁힌다.

    이 컨텍스트 매니저의 주된 일은 :func:`~app.core.execution_context.execution_scope`
    로 ``stage``/``agent``/``iteration`` 을 좁히는 것이다 — Langfuse generation 이름과
    로그 상관 필드가 여기서 정해진다.

    단계 경계 줄은 **DEBUG 로컬 진단**이다(이슈 #53). AI 실행의 단계별 소요시간과
    중단 지점은 Langfuse 가 담당하고, Elasticsearch 로 나가는 것은
    :mod:`app.core.operational_logging` 의 운영 이벤트뿐이다. 본문은 어느 쪽에도
    남기지 않는다.

    ``yield`` 하는 dict 에 값을 넣으면 종료 로그에 함께 실린다. 결과 건수처럼 단계를
    마쳐야 아는 값을 줄 하나 더 쓰지 않고 남기기 위한 자리다::

        with stage_span(logger, ExecutionStage.EVENT_AGENT, agent="location") as outcome:
            result = run()
            outcome["candidateCount"] = len(result.candidates)

    실패해도 예외를 삼키지 않는다. 실패 원인과 errorCode 는 ``report_error`` 가
    따로 남기고, 여기서는 단계가 어디서 끊겼는지만 닫아 준다.
    """

    outcome: dict[str, Any] = {}
    with execution_scope(stage, agent=agent, iteration=iteration):
        started = perf_counter()
        logger.debug("단계 시작: %s", stage.value, extra=log_fields(**fields))
        try:
            yield outcome
        except BaseException:
            logger.debug(
                "단계 중단: %s",
                stage.value,
                extra=log_fields(
                    **{
                        **fields,
                        **outcome,
                        "durationMs": round((perf_counter() - started) * 1000, 3),
                    }
                ),
            )
            raise
        logger.debug(
            "단계 완료: %s",
            stage.value,
            extra=log_fields(
                **{
                    **fields,
                    **outcome,
                    "durationMs": round((perf_counter() - started) * 1000, 3),
                }
            ),
        )
