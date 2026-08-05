"""프로젝트 오류 코드 카탈로그.

이 서버가 밖으로 내보내는 실패는 **정수 하나로 식별한다**. API 응답·App Server
완료 콜백·운영 로그·관측 이벤트가 같은 실패에 같은 정수를 쓰게 하려고, 코드와
외부 메시지와 HTTP 상태를 여기 한 곳에서만 정의한다. 다른 모듈은 이 카탈로그를
참조만 하고 자기 문자열/숫자를 만들지 않는다.

계약 형태는 다음 두 필드다.

    {"errorCode": 1201, "error": "타임라인 생성이 제한 시간을 초과했습니다."}

``error`` 는 **여기 정의된 메시지만** 나간다. 원본 예외 메시지(``str(exc)``)는
내부 구현·경로·식별자를 흘리므로 로그에만 남긴다.

## 대역 규칙

코드는 영역별 100단위 대역이다. 코드만 봐도 어디서 깨졌는지 드러나고, 한 영역에
코드를 더해도 다른 영역과 충돌하지 않는다.

===========  ==========================================================
1000~1099    요청/입력 계약 — 클라이언트가 보낸 것이 계약을 어긴 경우
1100~1199    원본 스냅샷 — App Server 입력 조회 API 응답/계약
1200~1299    AI/LLM — 에이전트 실행, LLM 호출, 구조화 출력
1300~1399    결과 저장 — App Server 결과 저장 API, 저장 전 자체검증
1400~1499    외부 연동 — App Server 콜백, 인증/순서 거절
1900~1999    미분류 — 위 어디에도 속하지 않는 내부 오류
===========  ==========================================================

## 새 코드를 추가할 때

1. 해당 영역 대역에서 **비어 있는 가장 작은 번호**를 쓴다. 번호는 재사용하지 않는다
   (한번 나간 코드는 클라이언트·로그 집계에 남는다).
2. ``message`` 는 사용자/클라이언트에게 그대로 보여도 안전한 문장이어야 한다.
   경로·쿼리·식별자·예외 클래스명·스택을 넣지 않는다.
3. HTTP 응답으로 나갈 수 있는 코드만 ``http_status`` 를 채운다. 백그라운드
   전용(콜백·흡수 경로) 코드는 ``None`` 이다.
4. 대역이 꽉 차면 새 대역을 열고 이 docstring 의 표에 추가한다.

값 중복은 import 시점에 ``_assert_unique_values`` 가 막는다. 같은 정수를 두 번
쓰면 서버가 뜨지 않는다 — 운영에 나간 뒤에 발견하는 것보다 낫다.
"""

from __future__ import annotations

from enum import IntEnum
from types import MappingProxyType


class ErrorCode(IntEnum):
    """외부로 나가는 실패를 식별하는 정수 코드.

    ``IntEnum`` 이라 그대로 JSON 정수로 직렬화되고 비교·집계도 정수처럼 된다.
    """

    # --- 1000~1099 요청/입력 계약 ---------------------------------------
    #: 요청 본문이 Pydantic 계약을 어겼다(FastAPI RequestValidationError).
    REQUEST_VALIDATION_FAILED = 1001
    #: 요청 형식 자체가 잘못됐다(JSON 파싱 실패 등 그 밖의 4xx).
    BAD_REQUEST = 1002
    #: 요청한 경로나 리소스가 없다.
    NOT_FOUND = 1003
    #: 경로는 있으나 허용되지 않은 HTTP 메서드다.
    METHOD_NOT_ALLOWED = 1004

    #: **예약(deprecated)**. 구 계약의 문자열 코드 ``"ERROR_1008"`` 가 쓰던 번호다.
    #: 그때는 모든 타임라인 실패가 이 코드 하나로 나갔다. 지금은 원인별 코드를
    #: 보내므로 어디서도 emit 하지 않으며, 번호 재사용을 막기 위해서만 남긴다.
    #: 분류되지 않은 최종 실패는 :data:`INTERNAL_ERROR` 를 쓴다.
    LEGACY_TIMELINE_GENERATION_FAILED = 1008

    # --- 1100~1199 원본 스냅샷 -------------------------------------------
    #: 입력 조회 API 가 요청 taskId 로 수집 원본을 돌려주지 않았다(404).
    SOURCE_SNAPSHOT_NOT_FOUND = 1101
    #: 수집 원본 묶음이 파이프라인 입력 계약을 어겼다(SourceBatchError).
    SOURCE_CONTRACT_VIOLATION = 1102
    #: 개별 수집 항목 정규화에 실패해 그 항목만 건너뛰었다(흡수 경로).
    SOURCE_ITEM_NORMALIZE_FAILED = 1103
    #: 요청 timezone 을 해석하지 못해 기본값(KST)으로 진행했다(흡수 경로).
    TIMEZONE_RESOLUTION_FAILED = 1104
    #: 입력 조회 API 호출이 실패했다(5xx/timeout 을 재시도까지 소진).
    SOURCE_FETCH_FAILED = 1105
    #: 입력 조회 응답의 ``userMemory`` 가 v1.0 계약을 어겼다. User Memory 는 보조
    #: context 라 이 실패로 타임라인을 멈추지 않는다 — 값 없이 진행한다(흡수 경로).
    USER_MEMORY_CONTRACT_VIOLATION = 1106

    # --- 1200~1299 AI/LLM ------------------------------------------------
    #: 메인 에이전트가 제한 시간 안에 끝나지 않았다.
    PIPELINE_TIMEOUT = 1201
    #: 구조화 출력이 스키마 검증을 통과하지 못했다(재시도 소진 포함).
    STRUCTURED_OUTPUT_INVALID = 1202
    #: LLM provider 호출이 실패했다.
    LLM_CALL_FAILED = 1203
    #: Event Agent 가 실패해 그 에이전트 결과 없이 진행했다(흡수 경로).
    EVENT_AGENT_FAILED = 1204
    #: Timeline Agent 가 실패해 빈 draft 로 진행했다(흡수 경로).
    TIMELINE_AGENT_FAILED = 1205
    #: Repair Agent 가 실패해 직전 정상 draft 로 진행했다(흡수 경로).
    REPAIR_AGENT_FAILED = 1206
    #: Repair 도구 실행이 실패했다. repair 전체를 멈추지는 않는다(흡수 경로).
    REPAIR_TOOL_FAILED = 1207
    #: draft 편집이 실패했다(대상 event 없음·필드 없음·스키마 위반).
    DRAFT_EDIT_FAILED = 1208
    #: 회고 유도 질문 생성이 실패했다. 질문 없이 타임라인을 저장한다(흡수 경로).
    QUESTION_GENERATION_FAILED = 1209

    # --- 1300~1399 결과 저장 ----------------------------------------------
    #: 저장 전 자체검증에서 계약 위반이 나왔다(TimelineValidationError).
    TIMELINE_STORAGE_VALIDATION_FAILED = 1301

    #: **예약(deprecated)**. AI 서버가 staging DB 에 직접 붙던 시절의 코드다(#40
    #: 이전). 지금은 모든 데이터 접근이 App Server API 라 이 코드를 낼 수 있는
    #: 경로가 없다. 번호 재사용을 막기 위해서만 남긴다 — DB 계열 실패는 이제
    #: :data:`TIMELINE_RESULT_SUBMIT_FAILED` 나 :data:`SOURCE_FETCH_FAILED` 다.
    DATABASE_ERROR = 1302

    #: 결과 저장 API 호출이 실패했다(5xx/timeout 을 재시도까지 소진).
    TIMELINE_RESULT_SUBMIT_FAILED = 1303

    # --- 1400~1499 외부 연동 ---------------------------------------------
    #: App Server 완료 콜백 전송이 실패했다.
    CALLBACK_SEND_FAILED = 1401

    #: **예약(deprecated)**. AI 서버가 관측 이벤트를 직접 Elasticsearch ``_bulk`` 로
    #: 보내던 시절의 코드다(#47 이전). 지금은 애플리케이션이 Elasticsearch 를 호출하지
    #: 않는다 — 운영 로그는 stdout JSON 을 Filebeat 가 실어 나르고, AI 실행 관측은
    #: Langfuse 가 맡는다. 번호 재사용을 막기 위해서만 남긴다.
    LEGACY_OBSERVATION_EXPORT_FAILED = 1402
    #: **예약(deprecated)**. 같은 이유로 남긴 관측 sink/observer 기록 실패 코드다.
    LEGACY_OBSERVATION_EMIT_FAILED = 1403

    #: App Server 가 taskToken 을 거절했다(401). 재시도하지 않고 중단한다.
    APP_SERVER_UNAUTHORIZED = 1404
    #: App Server 에 task 가 없거나 만료됐다(404, 입력 조회 외 경로).
    APP_SERVER_TASK_NOT_FOUND = 1405
    #: App Server 가 호출 순서 충돌로 거절했다(409). 재시도하지 않고 중단한다.
    APP_SERVER_CONFLICT = 1406
    #: 사진 이미지 다운로드에 실패했다(HTTP 오류·timeout·형식/크기 거부·URL 정책 위반).
    #: 해당 사진만 메타데이터 설명으로 대체하고 진행한다(흡수 경로).
    PHOTO_IMAGE_FETCH_FAILED = 1407

    # --- 1900~1999 미분류 -------------------------------------------------
    #: 위 어디에도 분류되지 않은 내부 오류.
    INTERNAL_ERROR = 1901


#: 코드 → 외부로 내보내도 안전한 메시지.
#:
#: 여기 있는 문자열만 API ``error`` 필드와 콜백 ``error`` 필드로 나간다.
#: 내부 식별자·경로·예외 메시지를 넣지 않는다.
_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.REQUEST_VALIDATION_FAILED: "요청 형식이 올바르지 않습니다.",
    ErrorCode.BAD_REQUEST: "잘못된 요청입니다.",
    ErrorCode.NOT_FOUND: "요청한 리소스를 찾을 수 없습니다.",
    ErrorCode.METHOD_NOT_ALLOWED: "허용되지 않은 요청 방식입니다.",
    ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED: (
        "타임라인 생성 중 오류가 발생했습니다."
    ),
    ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND: "수집 데이터를 찾지 못했습니다.",
    ErrorCode.SOURCE_CONTRACT_VIOLATION: "수집 데이터가 올바르지 않습니다.",
    ErrorCode.SOURCE_ITEM_NORMALIZE_FAILED: "일부 수집 항목을 처리하지 못했습니다.",
    ErrorCode.TIMEZONE_RESOLUTION_FAILED: "요청 시간대를 해석하지 못했습니다.",
    ErrorCode.SOURCE_FETCH_FAILED: "수집 데이터를 가져오지 못했습니다.",
    ErrorCode.USER_MEMORY_CONTRACT_VIOLATION: (
        "사용자 메모리를 사용하지 못했습니다."
    ),
    ErrorCode.PIPELINE_TIMEOUT: "타임라인 생성이 제한 시간을 초과했습니다.",
    ErrorCode.STRUCTURED_OUTPUT_INVALID: "AI 응답을 해석하지 못했습니다.",
    ErrorCode.LLM_CALL_FAILED: "AI 모델 호출에 실패했습니다.",
    ErrorCode.EVENT_AGENT_FAILED: "일부 이벤트 분석에 실패했습니다.",
    ErrorCode.TIMELINE_AGENT_FAILED: "타임라인 구성에 실패했습니다.",
    ErrorCode.REPAIR_AGENT_FAILED: "타임라인 보정에 실패했습니다.",
    ErrorCode.REPAIR_TOOL_FAILED: "타임라인 보정 작업에 실패했습니다.",
    ErrorCode.DRAFT_EDIT_FAILED: "타임라인 수정에 실패했습니다.",
    ErrorCode.QUESTION_GENERATION_FAILED: "기록 질문 생성에 실패했습니다.",
    ErrorCode.TIMELINE_STORAGE_VALIDATION_FAILED: "타임라인 저장 검증에 실패했습니다.",
    ErrorCode.DATABASE_ERROR: "데이터 저장 중 오류가 발생했습니다.",
    ErrorCode.TIMELINE_RESULT_SUBMIT_FAILED: "타임라인 결과 저장에 실패했습니다.",
    ErrorCode.CALLBACK_SEND_FAILED: "결과 통보에 실패했습니다.",
    ErrorCode.LEGACY_OBSERVATION_EXPORT_FAILED: "관측 데이터 전송에 실패했습니다.",
    ErrorCode.LEGACY_OBSERVATION_EMIT_FAILED: "관측 데이터 기록에 실패했습니다.",
    ErrorCode.APP_SERVER_UNAUTHORIZED: "작업 인증에 실패했습니다.",
    ErrorCode.APP_SERVER_TASK_NOT_FOUND: "작업을 찾을 수 없습니다.",
    ErrorCode.APP_SERVER_CONFLICT: "작업 처리 순서가 맞지 않습니다.",
    ErrorCode.PHOTO_IMAGE_FETCH_FAILED: "사진 이미지를 가져오지 못했습니다.",
    ErrorCode.INTERNAL_ERROR: "타임라인 생성 중 오류가 발생했습니다.",
}

#: 코드 → HTTP 응답 상태. HTTP 로 나갈 수 있는 코드만 갖는다.
#: 백그라운드 전용(콜백·흡수) 코드는 여기 없고, 조회 시 500 으로 떨어진다.
_HTTP_STATUSES: dict[ErrorCode, int] = {
    ErrorCode.REQUEST_VALIDATION_FAILED: 422,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: 새 코드가 써서는 안 되는 예약 번호. 구 계약이 쓰던 값이라 재사용하면
#: 과거 로그·클라이언트 분기와 의미가 충돌한다. 여기 든 코드는 실패 콜백에도
#: 실을 수 없다 — 지금은 어떤 경로도 이 코드를 만들지 않는다.
RESERVED_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED,
        ErrorCode.DATABASE_ERROR,
        ErrorCode.LEGACY_OBSERVATION_EXPORT_FAILED,
        ErrorCode.LEGACY_OBSERVATION_EMIT_FAILED,
    }
)


def _assert_unique_values() -> None:
    """같은 정수를 두 코드가 쓰고 있으면 import 시점에 터뜨린다.

    ``IntEnum`` 은 값이 같은 멤버를 조용히 별칭(alias)으로 만든다. 그대로 두면
    서로 다른 실패가 같은 코드로 나가는데도 아무도 모른다. ``__members__`` 는
    별칭까지 포함하므로 정규 멤버 수와 비교해 잡아낸다.
    """

    aliases = [
        name for name, member in ErrorCode.__members__.items() if member.name != name
    ]
    if aliases:
        raise RuntimeError(
            f"ErrorCode 값이 중복됐습니다: {aliases}. 각 코드는 고유한 정수여야 합니다."
        )


def _assert_messages_complete() -> None:
    """메시지가 빠진 코드가 있으면 import 시점에 터뜨린다.

    메시지 없는 코드는 외부에 빈 ``error`` 를 내보내게 되는데, 계약이 ``error`` 를
    "비어 있지 않은 문자열"로 정의하므로 그 순간 계약 위반이다.
    """

    missing = sorted(code.name for code in ErrorCode if not _MESSAGES.get(code))
    if missing:
        raise RuntimeError(
            f"ErrorCode 에 외부 메시지가 없습니다: {missing}. "
            "모든 코드는 비어 있지 않은 안전 메시지를 가져야 합니다."
        )


_assert_unique_values()
_assert_messages_complete()

#: 읽기 전용 뷰. 카탈로그를 밖에서 수정하지 못하게 막는다.
ERROR_MESSAGES = MappingProxyType(dict(_MESSAGES))
ERROR_HTTP_STATUSES = MappingProxyType(dict(_HTTP_STATUSES))

#: HTTP 상태가 지정되지 않은 코드의 응답 상태.
DEFAULT_HTTP_STATUS = 500


def message_for(code: ErrorCode) -> str:
    """코드에 대응하는 외부 안전 메시지를 돌려준다."""

    return ERROR_MESSAGES[code]


def http_status_for(code: ErrorCode) -> int:
    """코드에 대응하는 HTTP 응답 상태를 돌려준다.

    HTTP 로 나갈 일이 없던 코드(백그라운드 전용)가 어쩌다 응답 경로에 닿으면
    ``500`` 으로 떨어진다. 내부 실패를 4xx 로 보여 클라이언트가 재시도를 포기하게
    만드는 것보다 낫다.
    """

    return ERROR_HTTP_STATUSES.get(code, DEFAULT_HTTP_STATUS)


def code_for_http_status(status_code: int) -> ErrorCode:
    """``HTTPException`` 등 상태 코드만 있는 실패를 카탈로그 코드로 옮긴다.

    전용 코드가 있는 상태(404/405/422)는 그것을 쓰고, 나머지 4xx 는
    :data:`ErrorCode.BAD_REQUEST`, 5xx 와 그 밖은 :data:`ErrorCode.INTERNAL_ERROR`
    로 모은다.
    """

    for code, mapped_status in _HTTP_STATUSES.items():
        if mapped_status == status_code and code is not ErrorCode.INTERNAL_ERROR:
            return code
    if 400 <= status_code < 500:
        return ErrorCode.BAD_REQUEST
    return ErrorCode.INTERNAL_ERROR
