"""확정된 ``TimelineDraft`` 를 App Server 결과 저장 요청으로 옮긴다.

내부 draft 와 저장 계약은 폭이 다르다(:mod:`app.schemas.timeline_result`).
좁히는 규칙을 한 곳에 모아 둔다.

- ``subtitle`` 은 draft 의 ``description`` 이다. 비어 있으면 ``null`` 로 보낸다.
- ``sourceRawIds`` 는 ``sourceRefs`` 의 ``rawId`` 를 **순서를 지키며** 디듀프한다.
  같은 근거를 두 번 참조한 이벤트가 App Server 에 중복 링크를 만들지 않게 한다.
- ``title``/``subtitle`` 은 255자로 자른다. App Server 저장 컬럼 한도이며, 이슈
  #40 이전 DB 직접 저장 때도 같은 값으로 잘랐다. 길이 때문에 저장 전체가
  거절되는 것보다 잘라 보내는 편이 낫다.
- 시각은 draft timezone 기준 offset 을 붙여 보낸다. 계약 예시가 ``+09:00`` 형태고,
  UTC(``Z``)로 보내면 같은 시각이라도 App Server 로그에서 사람이 대조하기 어렵다.
"""

from datetime import datetime, tzinfo

from app.schemas.timeline import TimelineDraft
from app.schemas.timeline_result import TimelineResultEvent, TimelineResultRequest
from app.services.validator import resolve_timezone

_TITLE_MAX = 255
_SUBTITLE_MAX = 255


def build_result_request(draft: TimelineDraft) -> TimelineResultRequest:
    """확정 draft 를 결과 저장 요청 body 로 만든다."""

    tz = resolve_timezone(draft.timezone)
    events = [
        TimelineResultEvent(
            event_type=event.event_type,
            title=event.title[:_TITLE_MAX],
            subtitle=_subtitle(event.description),
            start_at=_localized(event.start_time, tz),
            end_at=_localized(event.end_time, tz),
            source_raw_ids=_unique_raw_ids(event),
        )
        for event in draft.events
    ]
    return TimelineResultRequest(events=events)


def _subtitle(description: str | None) -> str | None:
    text = (description or "").strip()
    return text[:_SUBTITLE_MAX] if text else None


def _unique_raw_ids(event) -> list[str]:
    seen: set[str] = set()
    raw_ids: list[str] = []
    for ref in event.source_refs:
        if ref.raw_id in seen:
            continue
        seen.add(ref.raw_id)
        raw_ids.append(ref.raw_id)
    return raw_ids


def _localized(value: datetime, tz: tzinfo) -> datetime:
    """aware datetime 을 draft timezone 으로 옮긴다(시각 자체는 그대로다)."""

    return value.astimezone(tz)
