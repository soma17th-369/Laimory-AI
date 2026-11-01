"""App Server 결과 저장 API 요청 계약.

``POST {API_BASE}/timeline/drafts/{taskId}/result`` 로 보내는 형태다. 이슈 #40
이전에는 같은 결과를 AI 서버가 staging DB(`timeline_events`/`timeline_items`/
`timeline_event_items`)에 직접 INSERT 했다.

이 계약은 내부 :class:`~app.schemas.timeline.TimelineDraft` 보다 **좁다**. 근거
``rawId`` 목록과 사람이 읽는 필드만 나가고, ``confidence``/``inferenceLevel``/
``questions``/``warnings`` 같은 내부 판단 값은 App Server 로 넘기지 않는다. 저장
스키마는 App Server 소유이므로 AI 서버가 채울 수 있는 것만 계약에 둔다.

여기서 나가지 않는 ``questions`` 는 **모호성 확인 질문**(:class:`~app.schemas.
timeline.TimelineQuestion`)이다. 이름이 비슷한 :attr:`TimelineResultEvent.question`
은 그것과 다른 값으로, 사용자가 읽고 답하는 **회고 유도 질문**이며 이 계약으로
나간다(이슈 #66).

``TimelineDraft`` → 이 요청으로의 변환은
:func:`app.services.timeline_result.build_result_request` 가 한다.
"""

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import CamelModel, Latitude, Longitude, RawId
from app.schemas.event_candidate import EventType


#: 문장의 장소 칸(App Server 공지 2026-10-29). 자리 표시는 이 하나뿐이다.
PLACE_SLOT = "{place}"


#: event 하나에 내보내는 후보의 최대 개수(사용자 결정 2026-10-30). App Server 공지의 권장은 2~4개다.
MAX_PLACE_CANDIDATES = 5

#: App Server 저장 컬럼 한도. 제목·문장·장소·주소·질문·후보 이름이 같다.
TEXT_MAX = 255


def _absent(value: object) -> bool:
    return value is None


class PlaceCandidate(CamelModel):
    """사용자가 고를 후보 장소 하나(App Server 공지 2026-10-29, 좌표는 명세 2026-10-30).

    좌표는 입력 `places` 의 그 원소 좌표를 그대로 싣는다. 입력 `places` 밖에서 만든 후보(일정 장소명)는
    좌표가 없어 키를 싣지 않는다. 둘 다 있거나 둘 다 없고, App Server 는 한쪽만 있거나 범위 밖이면
    결과 batch 전체를 400 으로 거절한다.
    """

    name: str = Field(min_length=1, max_length=TEXT_MAX)
    latitude: Latitude | None = Field(default=None, exclude_if=_absent)
    longitude: Longitude | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _validate_pair(self) -> "PlaceCandidate":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("candidate latitude and longitude must be sent together")
        return self


class TimelineResultEvent(CamelModel):
    """저장할 타임라인 이벤트 하나.

    ``sourceRawIds`` 는 입력 조회로 받은 ``rawId`` 만 담는다. 입력에 없는 값은
    저장 전 자체검증(:mod:`app.services.timeline_validator`)에서 걸러진다.

    ``question`` 은 이 event 하나에 붙는 회고 유도 질문이다. **event 를 가리키는
    식별자를 따로 두지 않고 중첩으로 참조를 대신한다** — 이 계약에는
    ``clientEventId`` 가 없어서 최상위 질문 목록은 event 를 안정적으로 가리킬 수
    없다(이슈 #66).

    ``place``/``address`` 는 draft 의 ``place``/``address`` 다(이슈 #72). 그전에는
    확정해 놓고도 내보내지 않아 Repair 가 근거로 확정한 장소가 저장되지 않고 버려졌다.
    ``place`` 는 사용자가 알아보는 장소명(``집``, ``두꺼비 감자탕 지산점``)이고
    ``address`` 는 입력 근거에 실재하는 주소만 담는다 — 근거로 뒷받침되지 않는 주소는
    ``place_resolver`` 가 저장 전에 지운다.

    ``latitude``·``longitude`` 는 event 의 **대표 지점** 좌표다(#175). 사용자가 실제로 있던
    위치이고, 이동 event 는 도착지다. 후보의 좌표는 그 후보 장소의 좌표로 따로 싣는다(지도 마커용,
    명세 2026-10-30). App Server 는 1번 후보에 좌표가 있으면 event 좌표를 그 값으로 덮어쓰고 2번
    이하 후보의 좌표는 쓰지 않는다. 그래서 AI 가 대표 좌표를 1번 후보에 맞출 필요가 없다. 둘 다
    있거나 둘 다 없다. App Server 는 한쪽만 온 결과를 통째로 거절한다.

    ``placeCandidates`` 는 장소가 애매할 때 사용자가 고를 후보 장소다(#171). 우선순위가
    높은 순서이고, App Server 는 후보가 하나라도 있으면 ``place`` 를 1번 후보로 덮어쓴다.
    그래서 장소를 다른 근거로 확정한 event 와, ``place`` 를 1번 후보로 바꿔서는 안 되는
    v3 세트는 후보를 싣지 않는다.

    새 필드 셋은 값이 없으면 body 에 키를 싣지 않는다. 위치 근거가 없는 event 의 body 가
    필드를 더하기 전과 같게 하려는 것이다. 기존 필드의 ``null`` 은 그대로 나간다.
    """

    event_type: EventType = Field(alias="eventType")
    title: str = Field(min_length=1, max_length=TEXT_MAX)
    subtitle: str | None = Field(default=None, max_length=TEXT_MAX)
    place: str | None = Field(default=None, max_length=TEXT_MAX)
    address: str | None = Field(default=None, max_length=TEXT_MAX)
    latitude: Latitude | None = Field(default=None, exclude_if=_absent)
    longitude: Longitude | None = Field(default=None, exclude_if=_absent)
    place_candidates: list[PlaceCandidate] | None = Field(
        default=None, alias="placeCandidates", max_length=MAX_PLACE_CANDIDATES, exclude_if=_absent
    )
    start_at: AwareDatetime = Field(alias="startAt")
    end_at: AwareDatetime = Field(alias="endAt")
    source_raw_ids: list[RawId] = Field(alias="sourceRawIds", min_length=1)
    question: str | None = Field(default=None, max_length=TEXT_MAX)

    @model_validator(mode="after")
    def _validate_range(self) -> "TimelineResultEvent":
        if self.end_at < self.start_at:
            raise ValueError("endAt must be greater than or equal to startAt")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be sent together")
        if PLACE_SLOT in (self.subtitle or "") and not (self.place or self.place_candidates):
            # App Server 는 칸이 남은 채 장소가 없는 결과를 통째로 400 으로 거절한다(#171).
            raise ValueError("subtitle with {place} needs a place or placeCandidates")
        return self


class TimelineResultRequest(CamelModel):
    """결과 저장 요청 body.

    이벤트가 0건이어도 보낸다 — "생성 결과가 없음" 도 확정된 결과이고, App Server
    가 그 사실을 알아야 기존 AI 생성분을 정리할 수 있다.
    """

    events: list[TimelineResultEvent] = Field(default_factory=list)
