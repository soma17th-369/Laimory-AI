"""App Server 입력 조회 API 응답 계약.

``GET {API_BASE}/timeline/drafts/{taskId}/input`` 이 돌려주는 형태다. 이슈 #40
이전에는 같은 데이터를 AI 서버가 staging DB(`timeline_draft_source_items`)에서
직접 읽었다.

이 모델은 **전송 계약**이고, 파이프라인 내부 계약은 그대로
:class:`~app.schemas.source_snapshot.CollectedSnapshot` 이다. 두 계약의 차이는
시간 창 필드 이름뿐이라(``window.startAt/endAt`` ↔ ``timelineWindow.startTime/
endTime``) :meth:`TimelineInputResponse.to_snapshot` 한 곳에서 옮긴다. 응답 형태가
바뀌어도 normalizer 이하가 흔들리지 않게 하기 위한 경계다.

``taskToken`` 은 이 응답 body 로도 내려온다. 새 값이 오면 그 뒤의 모든 요청은
갱신된 토큰을 쓴다(:class:`~app.services.app_server_client.TaskToken`). 토큰은
계약상 body 로 받고 인증은 ``Task-Token`` 헤더로만 한다.

계약은 두 겹이다(#102). :class:`TimelineInputPayload` 가 **입력 한 벌**(``taskId`` 포함)
을 선언하고, :class:`TimelineInputResponse` 가 거기에 ``taskToken`` 만 더한다. 동기 테스트
엔드포인트처럼 App Server 를 되부르지 않는 입구는 앞의 것을 상속해서 **같은 필드 선언을
그대로 재사용한다** — 필드를 손으로 다시 적으면 한쪽만 고쳐져 두 입구가 갈린다.
"""

from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.source_snapshot import (
    CollectedSnapshot,
    CollectedSourceItem,
    TimelineWindow,
)
from app.schemas.user_memory import UserMemory


class TimelineInputWindow(CamelModel):
    """입력 조회 응답의 시간 창. API 계약은 ``startAt``/``endAt`` 이름을 쓴다."""

    start_at: str = Field(alias="startAt")
    end_at: str = Field(alias="endAt")


class TimelineInputPayload(CamelModel):
    """타임라인 생성 입력 한 벌. **필드를 선언하는 유일한 자리다.**

    입력 조회 응답(:class:`TimelineInputResponse`)과 동기 테스트 요청
    (:class:`~app.api.v1.timeline_testing.TimelineTestRequest`)이 이걸 상속해서 **같은
    선언 하나**를 쓴다 — 필드를 손으로 다시 적으면 한쪽만 고쳐져 두 입구가 말없이 갈린다.

    ``taskId`` 는 여기 있다. **App Server 가 발행하고 AI 서버가 받는 값**이라 두 입구에
    공통이다. 갈리는 것은 ``taskToken`` 하나뿐이고, 그것만
    :class:`TimelineInputResponse` 에 있다 — 토큰은 AI 서버가 App Server 를 **되부를
    때** 쓰는 인증이라 되부르지 않는 입구에는 쓸 곳이 없다.

    ``userMemory`` 는 선택이며 이 필드만 **원본 dict 로 느슨하게 받는다**(#65).
    여기서 :class:`~app.schemas.user_memory.UserMemory` 로 직접 선언하면 보조 context
    필드 하나가 틀렸을 때 입력 전체가 계약 위반(1102)이 되어 타임라인이 죽는다. 검증은
    :meth:`parse_user_memory` 가 따로 하고, 실패는 호출 경계가 흡수한다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    record_date: str = Field(alias="recordDate")
    record_time_zone: str = Field(default="Asia/Seoul", alias="recordTimeZone")
    window: TimelineInputWindow | None = None
    user_memory: dict[str, Any] | None = Field(default=None, alias="userMemory")
    source_items: list[CollectedSourceItem] = Field(
        default_factory=list, alias="sourceItems"
    )

    def parse_user_memory(self) -> UserMemory | None:
        """``userMemory`` 를 v1.0 계약으로 검증한다.

        필드가 없거나 ``null`` 이면 ``None`` 이다 — User Memory 없이 처리하던 기존
        동작 그대로다(하위 호환).

        Raises:
            ValidationError: 알 수 없는 최상위 필드, 지원하지 않는 ``schemaVersion``,
                길이·개수 상한 위반. 호출부가 흡수한다(코드 1106).
        """

        if self.user_memory is None:
            return None
        return UserMemory.model_validate(self.user_memory)

    def to_snapshot(self, user_memory: UserMemory | None = None) -> CollectedSnapshot:
        """파이프라인 내부 계약(`CollectedSnapshot`)으로 옮긴다.

        ``user_memory`` 는 :meth:`parse_user_memory` 로 **검증에 성공한 값만** 받는다.
        여기서 다시 파싱하지 않는 이유는, 실패를 어떻게 처리할지(흡수·중단)가 이
        전송 계약이 아니라 호출 경계의 판단이기 때문이다.

        시간 창은 뒤에서 요청 ``window`` 로 덮어쓰지만, 요청 window 가 정본이라는
        규칙이 깨졌을 때 응답 값이 남아 있어야 관측에서 원인을 볼 수 있으므로
        여기서도 채워 둔다.
        """

        window = None
        if self.window is not None:
            window = TimelineWindow(
                start_time=self.window.start_at,
                end_time=self.window.end_at,
            )

        return CollectedSnapshot(
            task_id=self.task_id,
            record_date=self.record_date,
            record_time_zone=self.record_time_zone,
            user_memory=user_memory,
            timeline_window=window,
            source_items=list(self.source_items),
        )


class TimelineInputResponse(TimelineInputPayload):
    """입력 조회 응답 전체. 입력 한 벌에 ``taskToken`` 을 더한 것이다.

    ``taskToken`` 은 계약상 선택이다. 응답에 실려 오면 홀더를 갱신하고, 없으면
    직전 토큰을 계속 쓴다 — 없다고 실패로 보지 않는다.

    이 필드만 여기 있는 이유는, 토큰이 **AI 서버가 App Server 를 되부를 때** 쓰는
    인증이어서다. 되부르지 않는 입구에는 쓸 곳이 없다.
    """

    task_token: str | None = Field(default=None, alias="taskToken")
