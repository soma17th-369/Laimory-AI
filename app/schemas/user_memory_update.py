"""User Memory 갱신 접수·저장 계약 (#64).

App Server 가 **확정된 하루 타임라인**과 **기존 User Memory** 를 보내면, AI 서버가 전체
갱신본을 만들어 결과 저장 경로로 돌려준다. 호출은 두 개뿐이다.

===  ==========================================================  ==================
E1   ``POST {ai}/v1/user-memory``                                 이 모듈의 접수 계약
E2   ``POST {app}/user-memory/updates/{taskId}/result``           이 모듈의 저장 계약
===  ==========================================================  ==================

**콜백이 없다.** E2 한 번이 결과 전달과 종료 통보를 겸하며 성공·실패 모두 이 경로로
나간다. 타임라인(#40)처럼 "저장 200 뒤에 콜백" 하는 순서 계약이 여기에는 없다 —
호출이 하나라 지킬 순서가 없다.

## 접수 계약을 느슨하게 받는 이유

``eventType`` 은 enum 이 아니라 자유 문자열이고, ``endAt``·``subtitle``·``question``·
``memo``·``emotionType`` 은 모두 nullable 이며, 길이 상한은 여기서 강제하지 않는다.
App Server 는 **4xx 를 "미접수 확정"** 으로 읽고 작업을 폐기한 뒤 앱에 502 를 준다.
즉 우리가 422 를 내면 사용자에게는 *일기 저장 실패*로 보인다. 이벤트가 많은 정상적인
하루가 그렇게 되면 안 되므로, 크기는 거절 사유가 아니라 프롬프트 조립 단계의
잘라내기로 다룬다(:mod:`app.services.user_memory_limits`).

## `SAVED` 전이와 분리된다

E2 의 ``FAILED`` 는 **"User Memory 가 안 바뀌었다"** 는 뜻이지 "하루 기록 저장이
실패했다" 가 아니다. ``DailyRecord`` 의 ``DRAFT → SAVED`` 전이는 앱 → App Server
구간(E0)에서 이미 끝나 있다. 둘을 한 트랜잭션으로 묶으면 AI 실패가 사용자의 일기
저장을 되돌리게 되고, "실패해도 사용자 피해가 없다" 는 전제가 사라진다.
"""

from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.core.error_codes import RESERVED_CODES, ErrorCode, message_for
from app.schemas.common import CamelModel
from app.schemas.task import TaskStatus
from app.schemas.user_memory import UserMemory


class DailyTimelineEvent(CamelModel):
    """확정된 하루 타임라인의 event 하나.

    ``title``·``subtitle``·``question`` 은 **이 시스템의 타임라인 AI 가 쓴 문장**이고
    ``memo`` 만 사용자가 직접 쓴 글이다. 이 구분은 갱신 프롬프트가 지키며, 근거는
    :mod:`app.agents.user_memory.user_memory_agent` 에 적었다.
    """

    event_type: str = Field(alias="eventType", min_length=1)
    title: str = ""
    subtitle: str | None = None
    question: str | None = None
    memo: str | None = None
    start_at: AwareDatetime = Field(alias="startAt")
    #: 단일 시점 event 가 있어 nullable 이다.
    end_at: AwareDatetime | None = Field(default=None, alias="endAt")


class DailyTimeline(CamelModel):
    """하루치 확정 타임라인."""

    date: str = Field(min_length=1)
    record_time_zone: str = Field(default="Asia/Seoul", alias="recordTimeZone")
    #: 현재 App Server 가 항상 ``null`` 로 보낸다. 받아만 두고 쓰지 않는다.
    emotion_type: str | None = Field(default=None, alias="emotionType")
    events: list[DailyTimelineEvent] = Field(default_factory=list)


class UserMemoryUpdateRequest(CamelModel):
    """``POST /v1/user-memory`` 접수 body.

    ``taskId`` 는 형식을 검증하지 않는다(App Server 는 UUIDv7 을 쓰지만 그건 그쪽
    사정이다). ``taskToken`` 은 이 작업 내내 같은 값이다 — E2 가 유일한 호출이라
    응답 body 로 갱신할 기회 자체가 없다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    task_token: str = Field(alias="taskToken", min_length=1)
    #: 기존 User Memory. 최초 생성이면 ``None``.
    #:
    #: :class:`~app.schemas.user_memory.UserMemory` 로 직접 선언하지 않는다. 그러면
    #: 기존 값 하나가 계약을 어겼을 때 요청 전체가 422 로 죽고, 그 사용자는 이후
    #: 어떤 날도 메모리를 갱신하지 못한다. 검증은 :meth:`parse_user_memory` 가 따로
    #: 하고 실패는 접수 경계가 흡수한다(#65 의 입력 조회와 같은 구조다).
    user_memory: dict[str, Any] | None = Field(default=None, alias="userMemory")
    daily_timelines: list[DailyTimeline] = Field(
        default_factory=list, alias="dailyTimelines"
    )

    def parse_user_memory(self) -> UserMemory | None:
        """기존 User Memory 를 v1.0 계약으로 검증한다.

        ``None`` 이면 최초 생성이다. 계약 위반은 여기서 ``ValidationError`` 로 올리고,
        흡수 여부는 호출 경계가 정한다.
        """

        if self.user_memory is None:
            return None
        return UserMemory.model_validate(self.user_memory)


class UserMemoryUpdateResponse(CamelModel):
    """접수 응답(202). 최종 결과는 E2 로 통보한다."""

    task_id: str = Field(alias="taskId")
    status: TaskStatus


class UserMemoryResultRequest(CamelModel):
    """E2 body. **성공과 실패가 같은 경로로 나간다.**

    콜백을 없앤 대가로, 이 요청을 빠뜨리면 실패를 알릴 수단이 하나도 없다. 그래서
    runner 의 모든 실패 경로가 여기로 수렴한다.

    ``error`` 는 카탈로그의 **안전 메시지**만 담는다(원본 예외 메시지는 로그에만
    남는다). 이 body 는 AI 서버 밖으로 나가므로 내부 식별자·경로가 실리면 안 된다.
    """

    status: TaskStatus
    #: 성공 시의 전체 갱신본. 실패면 아예 싣지 않는다 — 부분 결과를 저장하지 않는다.
    user_memory: UserMemory | None = Field(default=None, alias="userMemory")
    error_code: int | None = Field(default=None, alias="errorCode")
    error: str | None = None

    @field_validator("status")
    @classmethod
    def validate_terminal_status(cls, value: TaskStatus) -> TaskStatus:
        """종료 통보이므로 terminal 상태만 허용한다."""

        if value not in {TaskStatus.SUCCESS, TaskStatus.FAILED}:
            raise ValueError("User Memory 결과 status는 SUCCESS 또는 FAILED여야 합니다.")
        return value

    @model_validator(mode="after")
    def validate_field_pairs(self) -> "UserMemoryResultRequest":
        """상태와 값/오류 필드의 짝을 맞춘다.

        성공인데 갱신본이 없으면 App Server 가 저장할 것이 없고, 실패인데 코드가
        없으면 왜 안 바뀌었는지 알 수 없다. 만드는 쪽의 실수를 여기서 잡는다.
        """

        if self.status is TaskStatus.SUCCESS:
            if self.user_memory is None:
                raise ValueError("성공 결과에는 userMemory 가 필요합니다.")
            if self.error_code is not None or self.error is not None:
                raise ValueError("성공 결과에는 errorCode/error 를 넣지 않습니다.")
            return self

        if self.user_memory is not None:
            raise ValueError("실패 결과에는 userMemory 를 넣지 않습니다.")
        if self.error_code is None or not (self.error or "").strip():
            raise ValueError(
                "실패 결과에는 errorCode(정수)와 비어 있지 않은 error 가 필요합니다."
            )
        try:
            code = ErrorCode(self.error_code)
        except ValueError as exc:
            raise ValueError(
                "실패 결과 errorCode는 오류 코드 카탈로그에 있어야 합니다."
            ) from exc
        if code in RESERVED_CODES:
            raise ValueError("예약된 errorCode는 실패 결과에 사용할 수 없습니다.")
        if self.error != message_for(code):
            raise ValueError(
                "실패 결과 error는 오류 코드 카탈로그의 안전 메시지여야 합니다."
            )
        return self

    @classmethod
    def success(cls, user_memory: UserMemory) -> "UserMemoryResultRequest":
        """성공 결과. 전체 갱신본을 그대로 싣는다."""

        return cls(status=TaskStatus.SUCCESS, user_memory=user_memory)

    @classmethod
    def failure(cls, code: ErrorCode) -> "UserMemoryResultRequest":
        """실패 결과. 코드에 묶인 안전 메시지만 싣는다."""

        return cls(
            status=TaskStatus.FAILED,
            error_code=int(code),
            error=message_for(code),
        )
