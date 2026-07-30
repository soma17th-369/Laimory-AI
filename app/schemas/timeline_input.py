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
"""

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.source_snapshot import (
    CollectedSnapshot,
    CollectedSourceItem,
    TimelineWindow,
)


class TimelineInputWindow(CamelModel):
    """입력 조회 응답의 시간 창. API 계약은 ``startAt``/``endAt`` 이름을 쓴다."""

    start_at: str = Field(alias="startAt")
    end_at: str = Field(alias="endAt")


class TimelineInputResponse(CamelModel):
    """입력 조회 응답 전체.

    ``taskToken`` 은 계약상 선택이다. 응답에 실려 오면 홀더를 갱신하고, 없으면
    직전 토큰을 계속 쓴다 — 없다고 실패로 보지 않는다.
    """

    task_id: str = Field(alias="taskId", min_length=1)
    task_token: str | None = Field(default=None, alias="taskToken")
    record_date: str = Field(alias="recordDate")
    record_time_zone: str = Field(default="Asia/Seoul", alias="recordTimeZone")
    window: TimelineInputWindow | None = None
    source_items: list[CollectedSourceItem] = Field(
        default_factory=list, alias="sourceItems"
    )

    def to_snapshot(self) -> CollectedSnapshot:
        """파이프라인 내부 계약(`CollectedSnapshot`)으로 옮긴다.

        ``userMemory`` 는 이 API 가 주지 않으므로 ``None`` 이다(DB 경로에서도
        없었으므로 동작은 같다). 시간 창은 뒤에서 요청 ``window`` 로 덮어쓰지만,
        요청 window 가 정본이라는 규칙이 깨졌을 때 응답 값이 남아 있어야 관측에서
        원인을 볼 수 있으므로 여기서도 채워 둔다.
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
            user_memory=None,
            timeline_window=window,
            source_items=list(self.source_items),
        )
