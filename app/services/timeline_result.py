"""확정된 ``TimelineDraft`` 를 App Server 결과 저장 요청으로 옮긴다.

내부 draft 와 저장 계약은 폭이 다르다(:mod:`app.schemas.timeline_result`).
좁히는 규칙을 한 곳에 모아 둔다.

- ``subtitle`` 은 draft 의 ``description`` 이다. 비어 있으면 ``null`` 로 보낸다.
- ``sourceRawIds`` 는 ``sourceRefs`` 의 ``rawId`` 를 **순서를 지키며** 디듀프한다.
  같은 근거를 두 번 참조한 이벤트가 App Server 에 중복 링크를 만들지 않게 한다.
- ``title`` 30자·``subtitle`` 120자를 넘기지 않는다(#67). 예전에는 App Server 저장
  컬럼 한도인 255자에서 단순 절단했는데, 그 값은 제품이 사용자에게 약속한 길이보다
  훨씬 느슨했다. 이제 `narrative_guard` 의 상한을 같은 정본으로 쓰고, 끊는 방식도
  문장·단어 경계를 우선하는 같은 함수를 쓴다. `RepairAgent` 가 이미 강제하므로 여기서
  잘릴 일은 없어야 하지만, 저장 경계에도 같은 보장을 둔다.
- 시각은 draft timezone 기준 offset 을 붙여 보낸다. 계약 예시가 ``+09:00`` 형태고,
  UTC(``Z``)로 보내면 같은 시각이라도 App Server 로그에서 사람이 대조하기 어렵다.
- ``SLEEP``·``WAKE_UP`` 은 보내지 않는다(#67). `repair_draft` 가 이미 뺐지만, 저장
  경계에도 같은 정책을 둔다 — repair 를 거치지 않는 경로가 생겨도 수면이 새지 않는다.
- ``placeLabel``·``address`` 는 draft 가 확정한 값을 그대로 옮긴다(#67). 근거가 없어
  비어 있으면 ``null`` 이다 — 여기서 장소를 만들어 내지 않는다.
"""

from datetime import datetime, tzinfo

from app.schemas.timeline import TimelineDraft
from app.schemas.timeline_result import TimelineResultEvent, TimelineResultRequest
from app.services.narrative_guard import (
    DESCRIPTION_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    shorten,
)
from app.services.sleep_exclusion import HIDDEN_EVENT_TYPES
from app.services.validator import resolve_timezone


def build_result_request(draft: TimelineDraft) -> TimelineResultRequest:
    """확정 draft 를 결과 저장 요청 body 로 만든다."""

    tz = resolve_timezone(draft.timezone)
    events = [
        TimelineResultEvent(
            event_type=event.event_type,
            title=shorten(event.title, TITLE_MAX_LENGTH),
            subtitle=_subtitle(event.description),
            start_at=_localized(event.start_time, tz),
            end_at=_localized(event.end_time, tz),
            place_label=_text_or_none(event.place_label),
            address=_text_or_none(event.address),
            source_raw_ids=_unique_raw_ids(event),
        )
        for event in draft.events
        if event.event_type not in HIDDEN_EVENT_TYPES
    ]
    return TimelineResultRequest(events=events)


def _text_or_none(value: str | None) -> str | None:
    """빈 문자열은 ``null`` 로 보낸다. 장소를 모른다는 것과 빈 값은 같은 뜻이다."""

    text = (value or "").strip()
    return text or None


def _subtitle(description: str | None) -> str | None:
    text = (description or "").strip()
    return shorten(text, DESCRIPTION_MAX_LENGTH) if text else None


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
