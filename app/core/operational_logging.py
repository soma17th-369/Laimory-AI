"""Elasticsearch 로 나가는 **운영 이벤트**의 유일한 통로 (이슈 #53).

이 서버의 로그는 두 종류다.

1. **운영 이벤트** — 여기 정의된 것만. FastAPI 서버를 운영하는 데 필요하다고
   코드가 명시한 이벤트이며, 표식(``event.dataset``)을 달고 나가 Filebeat 가
   Elasticsearch 로 옮긴다.
2. **일반 로그** — ``get_logger()`` 로 남기는 그 밖의 모든 줄. 로컬·컨테이너
   진단용이다. 표식이 없어 Filebeat 가 버린다.

경계를 표식 하나로 둔 이유는 **추가되는 쪽이 안전해야** 하기 때문이다. 로그 호출은
앞으로도 계속 늘어나는데, "이건 빼자" 를 매번 기억해야 하는 구조면 언젠가 사용자
콘텐츠가 섞인다. 여기 등록하지 않은 이벤트는 자동으로 수집 대상이 아니다.

## 필드 계약

이벤트마다 **허용 필드가 정해져 있다**(:data:`_ALLOWED_FIELDS`). 호출부가 그 밖의
이름을 넘기면 조용히 버리고, 버렸다는 사실만 일반 로그로 알린다. 값이 아니라
이름만 남긴다 — 버려진 값이 진단 로그로 새면 막은 의미가 없다.

허용 목록에 넣어도 되는 값은 **정수·열거형·불리언·소요시간·식별자**뿐이다.
사용자 원문(제목·장소·주소·파일명), 프롬프트/응답, URL, 토큰, 예외 원문과
traceback 은 어떤 이벤트에도 넣지 않는다. 예외 상세가 필요하면 ``errorCode`` 와
``errorType`` 까지다.

## 실패 격리

로깅이 요청 처리를 깨뜨리면 안 된다. :func:`emit_event` 는 어떤 이유로도 예외를
올리지 않는다.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from app.core.execution_context import current_execution_context
from app.core.logging import OPERATIONAL_ATTR, get_logger

#: 수집 대상 표식. Filebeat 는 이 값이 정확히 일치하는 이벤트만 통과시킨다.
#: 값을 바꾸면 수집기 설정(`docs/observability/filebeat.example.yml`)도 같이 바꿔야 한다.
EVENT_DATASET = "laimory.api"

#: ECS 이름을 그대로 쓴다. 대시보드가 사람이 읽는 한국어 `message` 대신 이 세 필드로
#: 이벤트를 고른다 — 문구는 언제든 다듬을 수 있지만 계약은 흔들리면 안 된다.
DATASET_FIELD = "event.dataset"
ACTION_FIELD = "event.action"
OUTCOME_FIELD = "event.outcome"

MARKER_FIELDS = (DATASET_FIELD, ACTION_FIELD, OUTCOME_FIELD)


class OperationalEvent(StrEnum):
    """Elasticsearch 로 나가는 이벤트 종류(``event.action``).

    값은 대시보드·저장된 검색의 계약이다. 늘릴 수는 있어도 이름을 바꾸지 않는다.
    """

    #: HTTP 요청 하나의 종료. 요청당 정확히 한 건.
    HTTP_REQUEST_COMPLETED = "http.request.completed"
    #: 서버 기동 완료 / 종료.
    SERVER_STARTED = "server.started"
    SERVER_STOPPED = "server.stopped"
    #: 202 로 접수한 Timeline 백그라운드 작업의 종료. 작업당 정확히 한 건.
    TIMELINE_TASK_COMPLETED = "timeline.task.completed"
    #: 202 로 접수한 User Memory 갱신 작업의 종료(#64). 작업당 정확히 한 건.
    USER_MEMORY_TASK_COMPLETED = "usermemory.task.completed"
    #: 외부 연동(App Server) 논리 호출 하나의 종료.
    DEPENDENCY_REQUEST_COMPLETED = "dependency.request.completed"
    #: 그 호출의 재시도 한 번.
    DEPENDENCY_REQUEST_RETRY = "dependency.request.retry"


class EventOutcome(StrEnum):
    """``event.outcome``. 성공/실패를 문구가 아니라 값으로 판단하게 한다."""

    SUCCESS = "success"
    FAILURE = "failure"


#: 이벤트별 허용 필드. 여기 없는 이름은 나가지 않는다.
_ALLOWED_FIELDS: dict[OperationalEvent, frozenset[str]] = {
    OperationalEvent.HTTP_REQUEST_COMPLETED: frozenset(
        {
            "method",
            "route",
            "httpStatus",
            "durationMs",
            "errorCode",
            "errorType",
            "taskId",
        }
    ),
    OperationalEvent.SERVER_STARTED: frozenset({"appEnv", "logFormat"}),
    OperationalEvent.SERVER_STOPPED: frozenset({"appEnv", "uptimeMs"}),
    OperationalEvent.TIMELINE_TASK_COMPLETED: frozenset(
        {
            "taskId",
            "status",
            "durationMs",
            "callbackSent",
            "errorCode",
            "failureStage",
        }
    ),
    OperationalEvent.USER_MEMORY_TASK_COMPLETED: frozenset(
        {
            "taskId",
            "status",
            "durationMs",
            # 콜백이 없는 계약이라 이 한 번이 통보의 전부다. False 면 App Server 는
            # 아무 연락도 못 받고 TTL 로 정리한다 — 가장 먼저 봐야 할 값이다.
            "resultSent",
            "errorCode",
            "hasExistingMemory",
            # 입력을 잘랐는지. 조용히 자르면 "다 보고 이 정도" 인지 "못 본 게 있어서
            # 이 정도" 인지 구분할 수 없다.
            "diaryCount",
            "eventCount",
            "memoCount",
            "droppedDiaryCount",
            "droppedEventCount",
            # 결과의 모양. 본문이 아니라 크기와 개수다.
            "repairAttempts",
            "schemaVersion",
            "filledFieldCount",
            "customAttributeCount",
            "serializedChars",
        }
    ),
    OperationalEvent.DEPENDENCY_REQUEST_COMPLETED: frozenset(
        {
            "dependency",
            "operation",
            "httpStatus",
            "attempts",
            "durationMs",
            "errorCode",
            "taskId",
            "tokenRefreshCount",
        }
    ),
    OperationalEvent.DEPENDENCY_REQUEST_RETRY: frozenset(
        {
            "dependency",
            "operation",
            "attempt",
            "maxAttempts",
            "reason",
            "httpStatus",
            "delayMs",
            "taskId",
        }
    ),
}

#: 사람이 읽는 고정 문구. 호출부가 문자열을 만들지 못하게 여기서 소유한다 —
#: 포맷 인자를 받는 순간 사용자 콘텐츠가 message 로 들어올 자리가 생긴다.
_MESSAGES: dict[OperationalEvent, str] = {
    OperationalEvent.HTTP_REQUEST_COMPLETED: "HTTP 요청 완료",
    OperationalEvent.SERVER_STARTED: "서버 기동 완료",
    OperationalEvent.SERVER_STOPPED: "서버 종료",
    OperationalEvent.TIMELINE_TASK_COMPLETED: "Timeline 작업 완료",
    OperationalEvent.USER_MEMORY_TASK_COMPLETED: "User Memory 갱신 작업 완료",
    OperationalEvent.DEPENDENCY_REQUEST_COMPLETED: "외부 연동 호출 완료",
    OperationalEvent.DEPENDENCY_REQUEST_RETRY: "외부 연동 호출 재시도",
}

#: 운영 이벤트는 이 로거 하나로만 나간다. `logger` 필드로 바로 구분되고, 호출부
#: 모듈이 늘어나도 수집 대상 로거가 흩어지지 않는다.
_LOGGER_NAME = "app.operational"

_logger = get_logger(_LOGGER_NAME)
#: 계약 위반(허용되지 않은 필드 등)을 알리는 자리. 일반 로그라 수집되지 않는다.
_diagnostic = get_logger(__name__)


def http_outcome(status_code: int) -> EventOutcome:
    """HTTP 상태 코드를 ``event.outcome`` 으로 옮긴다(4xx 부터 실패)."""

    return EventOutcome.FAILURE if status_code >= 400 else EventOutcome.SUCCESS


def http_level(status_code: int) -> int:
    """HTTP 상태 코드의 로그 레벨. 2xx/3xx=INFO, 4xx=WARNING, 5xx=ERROR."""

    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    return logging.INFO


def emit_event(
    event: OperationalEvent,
    *,
    outcome: EventOutcome = EventOutcome.SUCCESS,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """운영 이벤트 한 건을 Elasticsearch 수집 대상으로 기록한다.

    ``None`` 인 필드는 넣지 않는다(줄마다 ``null`` 을 채우면 매핑만 늘어난다).
    ``taskId`` 는 이벤트가 허용하면 실행 컨텍스트에서 자동으로 채운다.

    Args:
        event: 이벤트 종류. ``event.action`` 으로 나간다.
        outcome: 성공/실패.
        level: 로그 레벨. 실패 이벤트는 호출부가 WARNING/ERROR 로 올린다.
        **fields: 이벤트가 허용한 구조화 필드. 그 밖의 이름은 버린다.

    이 함수는 어떤 경우에도 예외를 올리지 않는다 — 로깅 실패가 요청 처리나
    백그라운드 작업을 깨뜨리면 안 된다.
    """

    try:
        payload = _build_payload(event, outcome, fields)
    except Exception:  # noqa: BLE001 - 관측이 처리를 깨뜨리지 않는다.
        _diagnostic.debug("운영 이벤트 조립 실패: %s", event.value, exc_info=True)
        return

    try:
        _logger.log(level, "%s", _MESSAGES[event], extra={OPERATIONAL_ATTR: payload})
    except Exception:  # noqa: BLE001 - 핸들러 오류까지 방어한다.
        _diagnostic.debug("운영 이벤트 기록 실패: %s", event.value, exc_info=True)


def _build_payload(
    event: OperationalEvent,
    outcome: EventOutcome,
    fields: dict[str, Any],
) -> dict[str, Any]:
    allowed = _ALLOWED_FIELDS[event]
    payload: dict[str, Any] = {
        DATASET_FIELD: EVENT_DATASET,
        ACTION_FIELD: event.value,
        OUTCOME_FIELD: outcome.value,
    }

    rejected = sorted(key for key in fields if key not in allowed)
    if rejected:
        # 값은 남기지 않는다. 버린 이유를 알려면 이름만으로 충분하고, 값을 남기면
        # 정확히 막으려던 것이 진단 로그로 새어 나간다.
        _diagnostic.debug(
            "운영 이벤트에 허용되지 않은 필드를 버렸습니다: event=%s, fields=%s",
            event.value,
            ", ".join(rejected),
        )

    for key in sorted(allowed):
        value = fields.get(key)
        if value is not None:
            payload[key] = value

    if "taskId" in allowed and "taskId" not in payload:
        context = current_execution_context()
        if context is not None:
            payload["taskId"] = context.task_id

    return payload
