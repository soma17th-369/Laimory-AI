"""App Server 결과 저장 API 요청 계약.

``POST {API_BASE}/timeline/drafts/{taskId}/result`` 로 보내는 형태다. 이슈 #40
이전에는 같은 결과를 AI 서버가 staging DB(`timeline_events`/`timeline_items`/
`timeline_event_items`)에 직접 INSERT 했다.

이 계약은 내부 :class:`~app.schemas.timeline.TimelineDraft` 보다 **좁다**. 근거
``rawId`` 목록과 사람이 읽는 필드만 나가고, ``confidence``/``inferenceLevel``/
``questions``/``warnings`` 같은 내부 판단 값은 App Server 로 넘기지 않는다. 저장
스키마는 App Server 소유이므로 AI 서버가 채울 수 있는 것만 계약에 둔다.

``TimelineDraft`` → 이 요청으로의 변환은
:func:`app.services.timeline_result.build_result_request` 가 한다.
"""

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import CamelModel, RawId
from app.schemas.event_candidate import EventType


class TimelineResultEvent(CamelModel):
    """저장할 타임라인 이벤트 하나.

    ``sourceRawIds`` 는 입력 조회로 받은 ``rawId`` 만 담는다. 입력에 없는 값은
    저장 전 자체검증(:mod:`app.services.timeline_validator`)에서 걸러진다.
    """

    event_type: EventType = Field(alias="eventType")
    title: str = Field(min_length=1)
    subtitle: str | None = None
    start_at: AwareDatetime = Field(alias="startAt")
    end_at: AwareDatetime = Field(alias="endAt")
    source_raw_ids: list[RawId] = Field(alias="sourceRawIds", min_length=1)

    @model_validator(mode="after")
    def _validate_range(self) -> "TimelineResultEvent":
        if self.end_at < self.start_at:
            raise ValueError("endAt must be greater than or equal to startAt")
        return self


class TimelineResultRequest(CamelModel):
    """결과 저장 요청 body.

    이벤트가 0건이어도 보낸다 — "생성 결과가 없음" 도 확정된 결과이고, App Server
    가 그 사실을 알아야 기존 AI 생성분을 정리할 수 있다.
    """

    events: list[TimelineResultEvent] = Field(default_factory=list)
