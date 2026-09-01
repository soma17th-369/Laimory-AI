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

필드 **이름**에도 제약이 하나 있다(이슈 #109). 최상위 이름은 수집기가 객체로 채우는
이름(``agent``·``host``·``container``·``log``·``ecs``·``input``·``error``)과 겹치면 안 된다.
겹치면 같은 이름이 한쪽에서는 객체, 한쪽에서는 문자열이 되어 Elasticsearch 가 그 문서를
거절한다 — 이벤트가 조용히 통째로 사라지는 실패다. Agent 이름을 ``agentName`` 으로 내보내는
이유가 그것이고, 이 규칙은 ``tests/scripts/test_filebeat_config.py`` 가 고정한다.

허용 목록에 넣어도 되는 값은 **정수·열거형·불리언·소요시간·식별자**뿐이다.
사용자 원문(제목·장소·주소·파일명), 프롬프트/응답, URL, 토큰은 어떤 이벤트에도
넣지 않는다.

**예외 원문과 traceback 은 예외다**(#109 범위 확장). 실패 이벤트 두 개
(``app.degraded``·``http.request.completed``)는 ``errorMessage`` 와
``errorStackTrace`` 를 싣는다. 원래 이 둘은 #53 이 명시적으로 막아 둔 값이고,
사용자 콘텐츠가 섞일 수 있다는 것을 알고 연 것이다 — prod 는 AgentCore 가 컨테이너를
회수해 ``docker logs`` 라는 선택지가 없어, 이것이 없으면 운영에서 원인을 볼 방법이
아예 없기 때문이다. 보호 경계는 이제 인덱스 접근 권한과 보존 정책이 맡는다.
**새 필드를 더할 때 이 둘을 선례로 삼지 않는다.** 값이 아니라 코드로 말할 수 있으면
코드로 말한다.

## 사람이 읽는 ``message`` (이슈 #78)

집계 계약은 ``event.action``/``event.outcome`` 이고 ``message`` 는 사람이 읽는 한
줄이다. 그래도 Kibana 목록에서 구조화 필드를 펼치기 전에 무슨 일이 있었는지 보여야
쓸모가 있다. 그래서 외부 연동 이벤트의 문구는 ``dependency``·``operation``·
``event.outcome`` 의 **고정 매핑**으로 정한다(:func:`_message_for`).

문구에 들어가는 값은 :data:`_DEPENDENCY_LABELS` 와 :data:`_OPERATION_LABELS` 의
상수뿐이다. 라벨을 모르는 값은 문구에 **넣지 않고** 일반 문구로 통째로 폴백한다.
호출부가 넘긴 문자열이 문구로 흘러가는 경로를 하나도 만들지 않는 것이 이 설계의
전부다 — 그 경로가 생기면 사용자 콘텐츠와 고카디널리티 값이 곧 따라 들어온다.

## 저하 이벤트 (이슈 #101)

`app.degraded` 는 **작업이 성공으로 끝나도** 나간다. 흡수 경계들이 예외를 삼키고
fallback 으로 진행하기 때문에, 그것 없이는 Event Agent 하나가 통째로 죽어도
`timeline.task.completed` 가 `status=SUCCESS` 로만 남는다.

:func:`emit_degraded` 가 그 통로다. 대부분은 :func:`~app.core.exceptions.report_error`
가 대신 불러 주므로 호출부가 따로 신경 쓰지 않는다. 항목 단위 루프처럼 한 작업에서
수십 건이 나오는 자리만 ``report_error(..., emit=False)`` 로 빼고 집계 1건으로 대신 낸다.

## 실패 격리

로깅이 요청 처리를 깨뜨리면 안 된다. :func:`emit_event` 는 어떤 이유로도 예외를
올리지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.execution_context import ExecutionStage, current_execution_context
from app.core.logging import OPERATIONAL_ATTR, get_logger
from app.core.redaction import redact_text

#: 수집 대상 표식. Filebeat 는 이 값이 정확히 일치하는 이벤트만 통과시킨다.
#: 값을 바꾸면 수집기 설정(`docs/observability/filebeat.example.yml`)도 같이 바꿔야 한다.
EVENT_DATASET = "laimory.api"

#: 이 프로세스를 가리키는 값. 기동 때 한 번 만들어지고 죽을 때까지 같다.
#:
#: AgentCore 는 유휴 컨테이너를 회수하고 필요할 때 새로 띄우므로, 한 log group 에 여러
#: 인스턴스의 줄이 섞여 들어온다. 이 값이 없으면 어느 컨테이너의 기록인지 가를 수 없고
#: cold start 를 셀 수도 없다. EC2 는 컨테이너가 오래 살아 사실상 배포마다 하나다.
INSTANCE_ID = str(uuid4())

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
    #: 무언가를 잃었지만 처리는 계속된 지점(이슈 #101).
    #:
    #: **작업이 성공으로 끝나도 나간다.** 흡수 경계들이 예외를 삼키고 fallback 으로
    #: 진행하므로 `timeline.task.completed` 는 `status=SUCCESS`, `errorCode` 없음으로
    #: 나가고, Event Agent 하나가 통째로 죽어도 운영에서는 성공한 작업으로만 보인다.
    #: 그 간극을 메우는 것이 이 이벤트다 — 이 둘을 같은 `taskId` 로 묶어 봐야
    #: "성공했지만 무엇을 잃었는지" 가 보인다.
    APP_DEGRADED = "app.degraded"


class EventOutcome(StrEnum):
    """``event.outcome``. 성공/실패를 문구가 아니라 값으로 판단하게 한다."""

    SUCCESS = "success"
    FAILURE = "failure"


class DegradedComponent(StrEnum):
    """``app.degraded`` 의 ``component`` 중 실행 단계로 표현되지 않는 것.

    저하 지점은 보통 :class:`~app.core.execution_context.ExecutionStage` 값으로 답한다.
    여기 있는 것은 **task 바깥**이라 단계가 없는 프로세스 수준 결함이다.
    """

    #: 시크릿 번들을 읽지 못해 환경변수·기본값으로 기동했다(#30).
    SECRET_BUNDLE = "secret-bundle"
    #: Langfuse 키가 없어 trace 를 남기지 않는다. 오류가 아니라 구성 상태다.
    LANGFUSE = "langfuse"
    #: 요청 window 를 파싱하지 못해 범위 검증을 통째로 건너뛰었다.
    #: 단계로 치면 `REQUEST` 지만 정규화 실패와 섞이면 구분할 수 없어 따로 둔다.
    WINDOW = "window"


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
            # 예외 원문과 traceback (#109 범위 확장). 모듈 docstring 의 경계 설명을 함께
            # 본다. traceback 은 **미처리 예외(500)에만** 실린다 — 분류된 실패는 코드가
            # 원인을 말하고, 404 마다 스택을 싣는 것은 크기만 늘린다.
            "errorMessage",
            "errorStackTrace",
            "taskId",
        }
    ),
    OperationalEvent.SERVER_STARTED: frozenset({"appEnv", "logFormat", "instanceId"}),
    OperationalEvent.SERVER_STOPPED: frozenset({"appEnv", "uptimeMs", "instanceId"}),
    OperationalEvent.TIMELINE_TASK_COMPLETED: frozenset(
        {
            "taskId",
            "status",
            "durationMs",
            "callbackSent",
            "errorCode",
            "failureStage",
            # 제한 시간이 끝나 개선을 못 끝낸 채 저장한 성공(이슈 #76). status 만으로는
            # 정상 완료와 구분되지 않고, errorCode 는 실패가 아니라 비어 있다.
            "timedOut",
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
            "dailyTimelineCount",
            "eventCount",
            "memoCount",
            "droppedDailyTimelineCount",
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
    OperationalEvent.APP_DEGRADED: frozenset(
        {
            # 어디가 저하됐는지. ExecutionStage 값이거나 DegradedComponent 값이다.
            "component",
            # 우리 Event/Repair Agent 이름. **`agent` 가 아니다**(#109) — ECS 와 Filebeat 는
            # `agent.*` 를 수집기 자신을 가리키는 객체로 쓴다. 같은 이름으로 문자열을 실으면
            # `decode_json_fields` 가 그 객체를 덮어써서 Elasticsearch 가 문서를 통째로
            # 거절한다(dev EC2 에서 이 이벤트만 적재되지 않았다).
            "agentName",
            "errorCode",
            # 예외 **클래스명**이다(`ThrottlingException` 등). 원문이 아니라 종류라
            # 사용자 데이터가 아니고, `http.request.completed` 도 같은 필드를 쓴다.
            # LLM 실패가 상위에서 1204 로 덮이는 경로가 있어(#101) 이 값이 없으면
            # 무엇이 터졌는지 ES 에서 알 수 없다.
            "errorType",
            # 예외 원문과 traceback (#109 범위 확장). 흡수된 실패는 코드가 상위에서
            # 1204 로 덮이는 일이 잦아, 무엇이 터졌는지 말할 수 있는 것이 이 둘뿐이다.
            "errorMessage",
            "errorStackTrace",
            "taskId",
            "durationMs",
            # 항목 단위 실패를 이벤트마다 내지 않고 집계 1건으로 낼 때의 개수.
            "droppedCount",
            # LLM 실패 진단(#101). `component=LLM` 일 때만 채워진다.
            "provider",
            "model",
            "providerVersion",
            # 구조화 출력 실패 진단(#98). 응답 본문이 아니라 종료 사유·블록 종류·토큰 수다.
            "stopReason",
            "contentBlockKinds",
            "tokenUsage",
        }
    ),
}

#: 오류 상세 필드의 길이 상한 (#109 범위 확장).
#:
#: docker json-file 은 16KB 를 넘는 줄을 **쪼갠다.** 그러면 "로그 한 줄은 유효한 JSON
#: 하나" 라는 계약이 깨져 이벤트가 통째로 사라지므로, 사람이 원인을 찾을 만큼만 남기고
#: 자른다. 자르기를 호출부에 맡기지 않는 이유는 한 곳만 잊어도 그 줄이 사라지기 때문이다.
ERROR_MESSAGE_MAX_CHARS = 1_000
ERROR_STACK_TRACE_MAX_CHARS = 6_000

#: 잘렸다는 사실 자체를 값에 남긴다. 없으면 "여기서 끝난 예외" 와 구분되지 않는다.
TRUNCATION_MARK = "…(잘림)"

#: 자를 필드와 (상한, 어느 쪽을 남길지). traceback 은 **뒤쪽**을 남긴다 — 마지막 프레임과
#: 예외 줄이 원인에 가깝고, 앞쪽은 매번 같은 진입 경로라 정보가 적다.
_TRUNCATED_FIELDS: dict[str, tuple[int, str]] = {
    "errorMessage": (ERROR_MESSAGE_MAX_CHARS, "head"),
    "errorStackTrace": (ERROR_STACK_TRACE_MAX_CHARS, "tail"),
}

#: `app.degraded` 가 호출부 `context` 에서 받아 실을 구조화 출력 진단 필드.
#: :meth:`~app.core.structured.ProviderStructuredOutputError.trace_fields` 가 만든다.
_LLM_TRACE_FIELDS = ("stopReason", "contentBlockKinds", "tokenUsage")

#: 사람이 읽는 기본 문구. 호출부가 문자열을 만들지 못하게 여기서 소유한다 —
#: 포맷 인자를 받는 순간 사용자 콘텐츠가 message 로 들어올 자리가 생긴다.
#: 외부 연동 이벤트는 여기 값을 **폴백**으로 쓰고, 라벨을 아는 호출은
#: :func:`_message_for` 가 더 구체적인 문구로 바꾼다(이슈 #78).
_MESSAGES: dict[OperationalEvent, str] = {
    OperationalEvent.HTTP_REQUEST_COMPLETED: "HTTP 요청 완료",
    # "서버" 가 아니라 "컨테이너" 다. AgentCore 에서는 배포 시점이 아니라 cold start
    # 마다 찍히므로, 오래 사는 서버를 가리키는 문구는 실제와 어긋난다.
    OperationalEvent.SERVER_STARTED: "컨테이너 기동 완료",
    OperationalEvent.SERVER_STOPPED: "컨테이너 종료",
    OperationalEvent.TIMELINE_TASK_COMPLETED: "Timeline 작업 완료",
    OperationalEvent.USER_MEMORY_TASK_COMPLETED: "User Memory 갱신 작업 완료",
    OperationalEvent.DEPENDENCY_REQUEST_COMPLETED: "외부 연동 호출 완료",
    OperationalEvent.DEPENDENCY_REQUEST_RETRY: "외부 연동 호출 재시도",
    OperationalEvent.APP_DEGRADED: "기능 저하",
}

#: 외부 연동 이벤트 문구에 쓰는 라벨. `dependency`/`operation` 값은 **여기 등록된
#: 것만** 문구가 된다. 등록되지 않은 값은 문구에 들어가지 않고 통째로 폴백한다 —
#: 그래야 App Server 가 새 operation 을 보내든 값이 오염되든 원문이 message 로
#: 새지 않는다. 라벨을 늘리는 것이 곧 지원하는 작업을 늘리는 것이다.
_DEPENDENCY_LABELS: dict[str, str] = {
    "app-server": "App Server",
}

_OPERATION_LABELS: dict[str, str] = {
    "input": "타임라인 입력 조회",
    "result": "타임라인 결과 저장",
    "callback": "타임라인 완료 콜백 전송",
    "user-memory-result": "User Memory 결과 저장",
}

#: 완료 이벤트 문구의 상태 접미사. 재시도 이벤트는 성공/실패가 아니라 재시도라는
#: 사실 자체가 상태라 이 표를 쓰지 않는다.
_OUTCOME_SUFFIXES: dict[EventOutcome, str] = {
    EventOutcome.SUCCESS: "성공",
    EventOutcome.FAILURE: "실패",
}

#: 문구를 구체화하는 대상. 여기 없는 이벤트는 `_MESSAGES` 문구를 그대로 쓴다.
_DEPENDENCY_EVENTS = frozenset(
    {
        OperationalEvent.DEPENDENCY_REQUEST_COMPLETED,
        OperationalEvent.DEPENDENCY_REQUEST_RETRY,
    }
)

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
        message = _message_for(event, outcome, payload)
    except Exception:  # noqa: BLE001 - 관측이 처리를 깨뜨리지 않는다.
        _diagnostic.debug("운영 이벤트 조립 실패: %s", event.value, exc_info=True)
        return

    try:
        _logger.log(level, "%s", message, extra={OPERATIONAL_ATTR: payload})
    except Exception:  # noqa: BLE001 - 핸들러 오류까지 방어한다.
        _diagnostic.debug("운영 이벤트 기록 실패: %s", event.value, exc_info=True)


def emit_degraded(
    component: ExecutionStage | DegradedComponent | str | None = None,
    *,
    error_code: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    error_stack_trace: str | None = None,
    agent: str | None = None,
    dropped_count: int | None = None,
    duration_ms: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_version: str | None = None,
    trace_fields: Mapping[str, Any] | None = None,
    level: int = logging.WARNING,
) -> None:
    """무언가를 잃었지만 계속 진행한 지점을 운영 이벤트로 남긴다(이슈 #101).

    ``component`` 와 ``agent`` 를 생략하면 현재 실행 컨텍스트에서 가져온다. 흡수 경계는
    대개 자기가 어느 단계인지 인자로 말하지 않기 때문이다.

    ``error_message``/``error_stack_trace`` 는 예외 원문과 traceback 이다(#109 범위
    확장). :func:`_bounded_error_text` 가 마스킹한 뒤 길이 상한까지 자른다.

    ``trace_fields`` 는 구조화 출력 진단(:data:`_LLM_TRACE_FIELDS`)만 골라 싣는다.
    호출부가 넘긴 dict 를 통째로 펴지 않는다 — 그 자리가 곧 사용자 콘텐츠가 들어오는
    통로가 된다. 어차피 allowlist 가 한 번 더 거르지만, 고르는 일을 여기서 끝낸다.

    :func:`emit_event` 와 같이 어떤 경우에도 예외를 올리지 않는다.
    """

    context = current_execution_context()
    if component is None and context is not None:
        component = context.stage
    if agent is None and context is not None:
        agent = context.agent

    fields: dict[str, Any] = {
        "component": component,
        # 인자 이름은 우리 도메인 어휘(`agent`)로 두고 나가는 이름만 `agentName` 이다.
        # 호출부가 아니라 수집 경로의 사정이라 그 경계를 여기서 끝낸다(#109).
        "agentName": agent,
        "errorCode": error_code,
        "errorType": error_type,
        # 마스킹·자르기는 `_build_payload` 가 한다. 여기서 하면 다른 호출부가 생길
        # 때마다 같은 상한을 다시 적어야 하고, 한 곳만 잊어도 그 줄이 통째로 사라진다.
        "errorMessage": error_message,
        "errorStackTrace": error_stack_trace,
        "droppedCount": dropped_count,
        "durationMs": duration_ms,
        "provider": provider,
        "model": model,
        "providerVersion": provider_version,
    }
    if trace_fields:
        for key in _LLM_TRACE_FIELDS:
            if key in trace_fields:
                fields[key] = trace_fields[key]

    emit_event(
        OperationalEvent.APP_DEGRADED,
        outcome=EventOutcome.FAILURE,
        level=level,
        **fields,
    )


def _message_for(
    event: OperationalEvent,
    outcome: EventOutcome,
    payload: dict[str, Any],
) -> str:
    """이벤트 한 건의 사람이 읽는 `message` 를 고른다(이슈 #78).

    외부 연동 이벤트는 `dependency`·`operation`·`event.outcome` 의 고정 매핑으로
    **어떤 작업이 어떻게 끝났는지**를 문구에 드러낸다. Kibana 목록에서 `operation`
    을 따로 펼치지 않아도 입력 조회·결과 저장·콜백·User Memory 결과 저장을 구분할 수
    있어야 하기 때문이다.

    문구에 들어가는 값은 **라벨 사전의 상수뿐이다.** payload 의 원본 문자열은 어떤
    경로로도 문구가 되지 않는다. 라벨을 모르는 dependency/operation 은 기존 일반
    문구로 통째로 폴백한다 — 알 수 없는 값을 문구에 실어 보내면 그 자리가 곧
    사용자 콘텐츠와 고카디널리티 값이 들어오는 통로가 된다.

    `event.action`/`event.outcome`/`operation` 필드는 건드리지 않는다. 집계 계약은
    여전히 그쪽이고 여기는 사람이 읽는 한 줄이다.
    """

    fallback = _MESSAGES[event]
    if event not in _DEPENDENCY_EVENTS:
        return fallback

    dependency_value = payload.get("dependency")
    operation_value = payload.get("operation")
    # 문자열이 아니면 라벨 조회조차 하지 않는다. 해시할 수 없는 값이 들어와
    # 여기서 터지면 이벤트 한 건이 통째로 사라진다 — 문구 하나 때문에 관측을
    # 잃지 않는다.
    if not isinstance(dependency_value, str) or not isinstance(operation_value, str):
        return fallback

    dependency = _DEPENDENCY_LABELS.get(dependency_value)
    operation = _OPERATION_LABELS.get(operation_value)
    if dependency is None or operation is None:
        return fallback

    if event is OperationalEvent.DEPENDENCY_REQUEST_RETRY:
        return f"{dependency} {operation} 재시도"
    return f"{dependency} {operation} {_OUTCOME_SUFFIXES[outcome]}"


def _bounded_error_text(key: str, value: Any) -> Any:
    """상한이 정해진 필드를 **마스킹한 뒤** 자른다. 그 밖의 값은 그대로 둔다.

    순서가 중요하다. 마스킹은 문자열을 줄이기만 하지 않는다 — ``a@b.co`` 가
    ``[REDACTED]`` 가 되듯 늘어날 수도 있어서, 자르고 나서 마스킹하면 상한이 실제로
    나가는 줄을 재지 못한다. 상한의 목적이 docker json-file 의 16KB 분할을 피하는
    것이라 자르는 대상은 최종 값이어야 한다.

    로그 포매터가 뒤에서 한 번 더 :func:`~app.core.redaction.redact_text` 를 건다.
    멱등이라 결과는 같고, 여기서 거는 것은 마스킹 책임을 옮기려는 것이 아니라
    **길이를 재기 위해서**다.

    문자열이 아니면 손대지 않는다 — 이 함수의 일은 줄 길이를 지키는 것이지 타입을
    바로잡는 것이 아니다.
    """

    rule = _TRUNCATED_FIELDS.get(key)
    if rule is None or not isinstance(value, str):
        return value

    value = redact_text(value)
    limit, keep = rule
    if len(value) <= limit:
        return value
    if keep == "tail":
        return TRUNCATION_MARK + value[-limit:]
    return value[:limit] + TRUNCATION_MARK


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
            payload[key] = _bounded_error_text(key, value)

    if "taskId" in allowed and "taskId" not in payload:
        context = current_execution_context()
        if context is not None:
            payload["taskId"] = context.task_id

    # 프로세스의 성질이라 호출부가 넘길 값이 아니다. `taskId` 와 같은 이유로 여기서 채운다.
    if "instanceId" in allowed:
        payload["instanceId"] = INSTANCE_ID

    return payload
