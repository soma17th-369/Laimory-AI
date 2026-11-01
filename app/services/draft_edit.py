"""draft event 수정·삭제·나누기·흡수 (결정론).

Repair Agent 가 "이 event 의 이 필드를 이렇게 고쳐라", "이 event 는 지워라", "이 event 를
이 시각에서 나눠라" 라고 말하면 실제 적용은 여기서 한다. LLM 이 draft 전체를 다시 써 내려가게 두지 않는
이유는 그러면 **손대지 않기로 한 event 까지 조용히 바뀌기** 때문이다. 수정은 지정한
event 의 지정한 필드에만 닿고, 나머지 값은 원본 그대로 남는다.

바꿀 수 있는 필드는 `_EDITABLE_FIELDS` 로 한정한다. `clientEventId` 는 편집 대상이
아니다. 그 id 는 확정 pass 가 주는 값이라(`validator.assign_event_ids`), LLM 이 임의로
바꾸면 같은 계획 안의 다음 도구 호출과 Question Agent 가 가리키는 곳이 어긋난다.

여기서는 **id 를 다시 매기지 않는다.** 한 번의 개선 계획이 여러 도구 호출을 담기
때문이다. 삭제할 때마다 번호를 다시 매기면 같은 계획 안의 다음 호출이 가리키는
`clientEventId` 가 다른 event 를 뜻하게 된다. 새로 생긴 event(split 조각)의 번호는 도구
실행이 모두 끝난 뒤 `repair_draft` 가 준다. 이미 준 번호는 Repair 가 끝날 때까지 바뀌지
않는다(#144).
"""

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
)
from app.schemas.clues import NotificationClue
from app.services.location_link import (
    LINK_REASON,
    activity_notification_ids,
    has_event_evidence,
    is_location_only,
)
from app.services.location_metrics import SHORT_STAY_MAX
from app.services.meal_guard import MEAL_MAX_DURATION, is_stay_meal
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

#: 하루의 어디에 있었는지를 말하는 근거.
_LOCATION_SOURCES = frozenset({EventSourceType.STAY, EventSourceType.MOVEMENT})

#: 대화 단서로 만든 event 의 confidence. 대화가 왔다는 것은 입력이 직접 말한다.
_CONVERSATION_CONFIDENCE = 0.7

#: Repair Agent 가 바꿀 수 있는 event 필드(입력 JSON 의 camelCase 이름).
_EDITABLE_FIELDS = frozenset(
    {
        "eventType",
        "title",
        "description",
        "address",
        "place",
        "tags",
        "startTime",
        "endTime",
        "confidence",
        "inferenceLevel",
        "sourceRefs",
        "uncertainty",
        # v4 의 장소 후보(#171). 확정 pass 가 근거의 후보 풀 밖의 이름을 버린다.
        "placeCandidates",
    }
)


class DraftEditError(AppError):
    """수정·삭제를 적용할 수 없을 때. 도구 계층이 잡아 실패 결과로 돌려준다."""

    default_code = ErrorCode.DRAFT_EDIT_FAILED


def find_event(draft: TimelineDraft, client_event_id: str) -> TimelineEventDraft:
    """`clientEventId` 로 event 를 찾는다. 없으면 `DraftEditError`."""

    for event in draft.events:
        if event.client_event_id == client_event_id:
            return event
    raise DraftEditError(f"clientEventId '{client_event_id}' 인 event 가 draft 에 없습니다.")


def update_event(
    draft: TimelineDraft,
    client_event_id: str,
    fields: dict[str, Any],
) -> TimelineEventDraft:
    """event 한 건의 지정한 필드만 바꾼다(in-place). 바뀐 event 를 돌려준다.

    스키마 검증(시간 순서·enum·confidence 범위 등)은 `TimelineEventDraft` 가 그대로
    한다. 검증에 실패하면 원본 event 를 **건드리지 않고** `DraftEditError` 를 던진다.
    잘못된 수정으로 event 를 반쯤 망가뜨리는 것보다, 고치지 못했다고 알리는 편이 낫다.
    """

    if not fields:
        raise DraftEditError("바꿀 필드가 없습니다.")

    unknown = sorted(set(fields) - _EDITABLE_FIELDS)
    if unknown:
        raise DraftEditError(
            f"바꿀 수 없는 필드입니다: {', '.join(unknown)}. "
            f"가능한 필드: {', '.join(sorted(_EDITABLE_FIELDS))}"
        )

    event = find_event(draft, client_event_id)
    payload = event.model_dump(by_alias=True, mode="json")
    payload.update(fields)
    payload["clientEventId"] = client_event_id  # id 는 편집 대상이 아니다

    try:
        updated = TimelineEventDraft.model_validate(payload)
    except ValidationError as exc:
        raise DraftEditError(f"수정한 event 가 스키마 검증에 실패했습니다: {exc}") from exc

    index = draft.events.index(event)
    draft.events[index] = updated
    logger.debug(
        "Repair: event 수정 clientEventId=%s, fields=%s",
        client_event_id,
        ", ".join(sorted(fields)),
    )
    return updated


def add_conversation_event(
    draft: TimelineDraft, clue: NotificationClue, title: str, description: str
) -> TimelineEventDraft:
    """대화 단서 하나로 대화 event 를 만든다(in-place, v4, #183).

    글은 호출자가 쓰고 시간과 근거는 그 단서에서 정한다. 근거는 그 대화 알림뿐이다. 도구가 아니면
    Repair 가 Timeline 의 잘못된 제외를 찾아도 대화를 되살릴 방법이 없었다. id 는 임시 값이고 확정
    pass 가 쓴 적 없는 새 번호를 준다.
    """

    if clue.first_at is None:
        raise DraftEditError(f"{clue.clue_id} 는 받은 시각을 읽을 수 없어 event 를 만들 수 없습니다.")
    try:
        event = TimelineEventDraft(
            client_event_id=f"conversation-{clue.clue_id}",
            event_type=EventType.SOCIAL,
            title=title,
            description=description,
            start_time=clue.first_at,
            end_time=clue.last_at or clue.first_at,
            confidence=_CONVERSATION_CONFIDENCE,
            inference_level=InferenceLevel.DIRECT,
            source_refs=[
                SourceRef(
                    source_type=EventSourceType.NOTIFICATION, raw_id=raw_id, reason="대화 알림"
                )
                for raw_id in clue.raw_ids
            ],
        )
    except ValidationError as exc:
        raise DraftEditError(f"대화 event 가 스키마 검증에 실패했습니다: {exc}") from exc
    draft.events.append(event)
    logger.debug("Repair: 대화 event 추가 clue=%s", clue.clue_id)
    return event


def delete_event(
    draft: TimelineDraft,
    client_event_id: str,
    request: TimelineDraftRequest | None = None,
) -> TimelineEventDraft:
    """event 한 건을 지운다(in-place). 지워진 event 를 돌려준다.

    `request` 를 주면(v3 세트) 그 시간에 어디 있었는지를 말하는 유일한 event 는 지우지
    않는다(#163). 지우려는 event 가 자기 체류·이동 근거를 가질 때만 본다 — 그 근거의 시간 중
    `SHORT_STAY_MAX` 를 넘는 구간을 위치 근거가 있는 다른 event 가 덮지 않으면 거절한다.
    Repair 가 알림 대화와 겹친다는 이유로 9.5시간 체류를 지웠고, 그 시간에는 알림 event 만
    남았다. 다른 event 와 겹친다는 것은 위치 근거를 지울 이유가 아니다.
    """

    event = find_event(draft, client_event_id)
    if request is not None:
        sole = _sole_location_span(event, draft, request)
        if sole is not None:
            raise DraftEditError(
                f"{client_event_id} 는 {sole[0]:%H:%M}~{sole[1]:%H:%M} 에 어디 있었는지를 "
                "말하는 유일한 event 라 지울 수 없습니다. 다른 event 와 시간이 겹친다는 것은 "
                "지울 이유가 아닙니다. 같은 방문을 그린 체류 카드면 absorb_location_event 로 "
                "흡수하고, 내용이 틀렸으면 update_event 로 고칩니다."
            )
    draft.events.remove(event)

    # 제목은 사용자 콘텐츠라 남기지 않는다. 무엇이 지워졌는지는 Langfuse 의 도구
    # 실행 기록에서 본다.
    logger.debug("Repair: event 삭제 clientEventId=%s", client_event_id)
    return event


def _sole_location_span(
    event: TimelineEventDraft, draft: TimelineDraft, request: TimelineDraftRequest
) -> tuple[datetime, datetime] | None:
    """event 의 자기 위치 근거 가운데 다른 위치 근거 event 가 덮지 않는 가장 긴 구간.

    `SHORT_STAY_MAX` 이하이면 `None` 이다. 확정 단계가 장소 근거로 붙인 체류(`LINK_REASON`)는
    그 event 가 댄 근거가 아니라 세지 않는다. 덮는 쪽은 붙인 체류여도 그 시간의 위치를 말한다.
    """

    times = _source_times(request)
    evidence = [
        (max(span[0], event.start_time), min(span[1], event.end_time))
        for ref in event.source_refs
        if ref.source_type in _LOCATION_SOURCES
        and ref.reason != LINK_REASON
        and (span := times.get(ref.raw_id)) is not None
    ]
    covers = sorted(
        (other.start_time, other.end_time)
        for other in draft.events
        if other is not event
        and any(ref.source_type in _LOCATION_SOURCES for ref in other.source_refs)
    )

    longest: tuple[datetime, datetime] | None = None
    for start, end in evidence:
        cursor = start
        gaps: list[tuple[datetime, datetime]] = []
        for cover_start, cover_end in covers:
            if cover_end <= cursor or cover_start >= end:
                continue
            if cover_start > cursor:
                gaps.append((cursor, cover_start))
            cursor = max(cursor, cover_end)
        if cursor < end:
            gaps.append((cursor, end))
        for gap in gaps:
            if gap[1] - gap[0] > SHORT_STAY_MAX and (
                longest is None or gap[1] - gap[0] > longest[1] - longest[0]
            ):
                longest = gap
    return longest


# --- 위치만 있는 event 흡수 (#138) ---------------------------------------------


def absorb_location_event(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    client_event_id: str,
    into_client_event_id: str,
) -> TimelineEventDraft:
    """위치만 있는 event 를 일정·사진 event 에 흡수한다(in-place). 흡수한 event 를 돌려준다.

    같은 방문을 두 번 그린 카드인지는 Repair 가 판단한다. 여기서는 정해진 흡수를 그대로
    적용한다 — 체류 근거와 함께 붙어 있던 보조 알림을 옮기고(디듀프), 대상의 시간을 체류까지 넓혀 하루에 빈칸이
    생기지 않게 한 뒤, 위치만 있는 event 를 지운다. 대상의 제목·본문·eventType 은 바꾸지
    않는다. 체류를 담아 고쳐 쓸 문장은 Repair 가 `update_event` 로 정한다.

    거절하는 경우(원본은 건드리지 않는다):

    - 흡수되는 쪽이 위치만 있는 event 가 아니다. 일정·사진이나 결제·예약 알림이 있는
      event 는 코드가 지우지 않는다(`is_location_only`).
    - 대상이 일정·사진 event 가 아니다.
    - 둘의 시간이 겹치지 않는다.
    - 대상이 `MEAL` 인데 넓힌 시간이 60분을 넘고, 흡수한 결과가 식당에 머문 식사
      (`is_stay_meal`: 그 체류 안에 사진이나 결제 알림이 있고 3시간 이하)가 아니다. 다음
      확정에서 `meal_guard` 가 잘라 체류의 나머지 시간이 하루에서 사라진다(#163).

    대상이 같은 체류를 확정 단계가 붙인 장소 근거(`LINK_REASON`)로 이미 갖고 있으면, 그 근거를
    흡수한 체류의 근거로 바꾼다. 표지가 남으면 그 체류가 식사의 근거로 세지지 않아 다음
    확정이 식사를 60분으로 자른다.
    """

    if client_event_id == into_client_event_id:
        raise DraftEditError("흡수되는 event 와 대상 event 가 같습니다.")

    source = find_event(draft, client_event_id)
    target = find_event(draft, into_client_event_id)

    if not is_location_only(source, activity_notification_ids(request)):
        raise DraftEditError(
            f"{client_event_id} 는 근거가 체류뿐인 event 가 아닙니다. 일정·사진이나 "
            "결제·예약 알림이 있는 event 는 흡수할 수 없습니다."
        )
    if not has_event_evidence(target):
        raise DraftEditError(
            f"{into_client_event_id} 는 일정이나 사진 근거가 있는 event 가 아닙니다."
        )
    if not (
        source.start_time <= target.end_time and target.start_time <= source.end_time
    ):
        raise DraftEditError("두 event 의 시간이 겹치지 않습니다.")

    start = min(source.start_time, target.start_time)
    end = max(source.end_time, target.end_time)
    absorbed_ids = {ref.raw_id for ref in source.source_refs}
    kept = [
        ref
        for ref in target.source_refs
        if not (ref.reason == LINK_REASON and ref.raw_id in absorbed_ids)
    ]
    cited = {ref.raw_id for ref in kept}
    refs = [*kept, *(ref for ref in source.source_refs if ref.raw_id not in cited)]

    absorbed = target.model_copy(
        update={"start_time": start, "end_time": end, "source_refs": refs}
    )
    if (
        target.event_type is EventType.MEAL
        and end - start > MEAL_MAX_DURATION
        and not is_stay_meal(absorbed, request)
    ):
        raise DraftEditError(
            "흡수하면 MEAL 이 60분을 넘는데, 그 체류 안에 식사의 사진이나 결제 알림이 없거나 "
            "3시간을 넘습니다. 식사 시간은 다음 확정에서 60분으로 잘려 나머지 체류 시간이 "
            "사라지므로, 이 체류는 흡수하지 말고 그대로 둡니다."
        )

    target.source_refs = refs
    target.start_time, target.end_time = start, end
    draft.events.remove(source)

    logger.debug(
        "Repair: 위치만 있는 event 흡수 clientEventId=%s → %s",
        client_event_id,
        into_client_event_id,
    )
    return target


# --- 나누기 (#119) -------------------------------------------------------------

#: 나눈 조각마다 Repair 가 정할 수 있는 필드. 근거(`sourceRefs`)는 여기 없다 — 코드가
#: 원본 시각을 보고 나눠 담는다.
_PART_FIELDS = frozenset(
    {
        "eventType",
        "title",
        "description",
        "place",
        # 주소는 받되 믿지 않는다. 근거에 없는 주소는 확정 pass 가 지운다. 받지 않으면
        # 조각 하나의 주소 때문에 나누기 전체가 실패하고, 실제 LLM 은 같은 호출을 되풀이했다.
        "address",
        "startTime",
        "endTime",
        "confidence",
        "inferenceLevel",
        "uncertainty",
        "tags",
        "placeCandidates",
    }
)

_PART_REQUIRED_FIELDS = ("title", "startTime", "endTime")

#: 구간 근거를 조각에 담는 기준. 겹친 길이가 근거나 조각의 절반 이상이어야 한다.
#: 경계에서 몇 분 걸친 이동까지 옆 조각에 담으면, 확정 pass 가 그 이동을 통째로 품도록
#: 조각의 시간을 늘려 나눈 경계가 무너진다.
_SHARED_OVERLAP_RATIO = 0.5


def _source_times(
    request: TimelineDraftRequest,
) -> dict[str, tuple[datetime, datetime]]:
    """rawId → 그 근거가 가리키는 시간. 사진·알림 같은 시점 근거는 시작과 끝이 같다."""

    tz = resolve_timezone(request.timezone)
    times: dict[str, tuple[datetime, datetime]] = {}

    def add(raw_id: str | None, start_text: str | None, end_text: str | None) -> None:
        if not raw_id or not start_text:
            return
        start = parse_datetime(start_text, tz)
        if start is None:
            return
        end = parse_datetime(end_text, tz) if end_text else None
        times[raw_id] = (start, end if end is not None and end >= start else start)

    for item in [*request.stays, *request.movements, *request.calendars, *request.healths]:
        add(item.raw_id, item.start_at, item.end_at)
    for photo in request.photos:
        add(photo.raw_id, photo.taken_at, None)
    for notification in request.notifications:
        add(notification.raw_id, notification.posted_at, None)
    return times


def _distance(
    span: tuple[datetime, datetime], part: tuple[datetime, datetime]
) -> timedelta:
    """두 구간 사이의 거리. 겹치거나 맞닿으면 0 이다."""

    if span[1] < part[0]:
        return part[0] - span[1]
    if span[0] > part[1]:
        return span[0] - part[1]
    return timedelta(0)


def _overlap(
    span: tuple[datetime, datetime], part: tuple[datetime, datetime]
) -> timedelta:
    return max(min(span[1], part[1]) - max(span[0], part[0]), timedelta(0))


def _assign_refs(
    refs: list[SourceRef],
    parts: list[tuple[datetime, datetime]],
    times: dict[str, tuple[datetime, datetime]],
) -> list[list[SourceRef]]:
    """원래 event 의 근거를 조각에 나눠 담는다. 어느 근거도 버리지 않는다.

    - 시점 근거(사진·알림)는 그 시각을 포함하는 조각 하나에만 담는다. 없으면 가장 가까운
      조각이다. 사진 한 장이 두 조각에 함께 들어가면 단일 귀속 계약이 깨진다.
    - 구간 근거(체류·이동·일정)는 충분히 겹치는 조각 모두에 담는다. 긴 체류 하나를
      오전·오후로 나누면 두 조각 다 그 체류가 근거다.
    - 시간을 읽을 수 없는 근거는 첫 조각에 담는다.
    """

    assigned: list[list[SourceRef]] = [[] for _ in parts]
    for ref in refs:
        span = times.get(ref.raw_id)
        if span is None:
            assigned[0].append(ref)
            continue

        # 같은 거리면 짧은 조각이 먼저다. 사진을 찍은 바로 그 순간의 조각이 있으면, 그
        # 시각에 끝나는 옆 조각이 아니라 그 조각이 사진을 가진다.
        nearest = min(
            range(len(parts)),
            key=lambda index: (
                -_overlap(span, parts[index]),
                _distance(span, parts[index]),
                parts[index][1] - parts[index][0],
                index,
            ),
        )
        if span[0] == span[1]:
            assigned[nearest].append(ref)
            continue

        length = span[1] - span[0]
        shared = [
            index
            for index, part in enumerate(parts)
            if (overlap := _overlap(span, part)) > timedelta(0)
            and (
                overlap >= length * _SHARED_OVERLAP_RATIO
                or overlap >= (part[1] - part[0]) * _SHARED_OVERLAP_RATIO
            )
        ]
        for index in shared or [nearest]:
            assigned[index].append(ref)
    return assigned


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S")


def _fit_into(
    parts: list[TimelineEventDraft], event: TimelineEventDraft
) -> list[TimelineEventDraft]:
    """조각을 원래 event 의 시간 안으로 맞춘다. 안쪽에 남는 것이 없는 조각은 뺀다.

    처음부터 길이가 0 인 조각(사진 순간)은 event 안에 있으면 그대로 둔다. 길이가 있던
    조각이 맞춘 뒤 0 이 됐다면 통째로 밖에 있던 것이다.
    """

    kept: list[TimelineEventDraft] = []
    for part in parts:
        start = max(part.start_time, event.start_time)
        end = min(part.end_time, event.end_time)
        had_length = part.end_time > part.start_time
        if end < start or (had_length and end == start):
            continue
        part.start_time, part.end_time = start, end
        kept.append(part)
    return kept


def _uncovered_spans(
    event: TimelineEventDraft,
    parts: list[tuple[datetime, datetime]],
    times: dict[str, tuple[datetime, datetime]],
) -> list[str]:
    """원래 event 의 시간 안에 있는데 어느 조각과도 겹치지 않는 구간 근거."""

    uncovered: list[str] = []
    for ref in event.source_refs:
        span = times.get(ref.raw_id)
        if span is None or span[0] == span[1]:
            continue  # 시점 근거는 가장 가까운 조각이 가진다
        inside = (max(span[0], event.start_time), min(span[1], event.end_time))
        if inside[1] <= inside[0]:
            continue  # event 의 시간 밖에 있는 근거다. 담을 조각이 있을 수 없다
        if any(_overlap(inside, part) > timedelta(0) for part in parts):
            continue
        uncovered.append(
            f"{ref.source_type.value} {_clock(inside[0])}~{_clock(inside[1])}"
        )
    return uncovered


def split_event(
    draft: TimelineDraft,
    client_event_id: str,
    parts: list[dict[str, Any]],
    request: TimelineDraftRequest,
) -> list[TimelineEventDraft]:
    """event 하나를 둘 이상으로 나눈다(in-place). 나눈 조각들을 돌려준다.

    조각마다 시간·타입·문장은 호출자가 준다. 무엇을 했는지는 조각마다 다르고 그것을 쓰는
    것은 의미 판단이다. **근거는 코드가 나눠 담는다.** 호출자에게 맡기면 빠뜨리는 근거가
    생기고, 사진이나 캘린더 일정이 빠지면 확정 pass 가 다른 event 를 새로 만들어 되살린다.

    나누기는 원래 event 의 시간 안에서만 한다. 밖으로 걸친 조각은 안쪽만 남기고, 통째로
    밖에 있는 조각은 뺀다. 요청 시간 범위 끝에서 잘린 event 는 근거가 그 뒤까지 이어져
    있어, 근거의 시간을 그대로 옮긴 조각이 event 밖으로 나가기 쉽다. 이것을 오류로
    돌려주자 실제 LLM 은 같은 호출을 세 번 되풀이하고 반복 횟수를 다 썼다.

    조각끼리 겹치거나, 남은 조각이 둘보다 적거나, 원래 event 의 구간 근거를 담을 조각이
    없으면 원본을 **건드리지 않고** `DraftEditError` 를 던진다. 마지막 것은 조각이 덮지
    않은 시간의 체류나 이동이 하루에서 조용히 사라지는 것을 막는다.

    조각의 id 는 임시 값이다(`event-003-1`). 확정 pass 가 쓴 적 없는 새 번호를 준다.
    """

    event = find_event(draft, client_event_id)

    if not isinstance(parts, list) or len(parts) < 2:
        raise DraftEditError("parts 는 조각을 둘 이상 담은 목록이어야 합니다.")

    base = event.model_dump(by_alias=True, mode="json")
    drafts: list[TimelineEventDraft] = []
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise DraftEditError(f"조각 {index} 은 객체여야 합니다.")
        unknown = sorted(set(part) - _PART_FIELDS)
        if unknown:
            raise DraftEditError(
                f"조각 {index} 에 쓸 수 없는 필드가 있습니다: {', '.join(unknown)}. "
                f"가능한 필드: {', '.join(sorted(_PART_FIELDS))}"
            )
        missing = [name for name in _PART_REQUIRED_FIELDS if not part.get(name)]
        if missing:
            raise DraftEditError(
                f"조각 {index} 에 필요한 필드가 없습니다: {', '.join(missing)}"
            )

        payload = {
            **base,
            # 장소와 주소는 조각마다 다를 수 있다. 비워 두면 확정 pass 가 그 조각의
            # 근거에서 다시 채운다.
            "place": None,
            "address": None,
            "question": None,
            **part,
            "clientEventId": f"{client_event_id}-{index}",
        }
        try:
            drafts.append(TimelineEventDraft.model_validate(payload))
        except ValidationError as exc:
            raise DraftEditError(
                f"조각 {index} 이 스키마 검증에 실패했습니다: {exc}"
            ) from exc

    drafts.sort(key=lambda item: (item.start_time, item.end_time))
    drafts = _fit_into(drafts, event)
    if len(drafts) < 2:
        raise DraftEditError(
            f"원래 event 의 시간({_clock(event.start_time)}~{_clock(event.end_time)}) "
            "안에 남는 조각이 둘보다 적습니다. 나누기는 원래 event 안에서만 합니다."
        )
    for left, right in zip(drafts, drafts[1:]):
        if right.start_time < left.end_time:
            raise DraftEditError("조각끼리 시간이 겹칩니다.")

    times = _source_times(request)
    part_spans = [(item.start_time, item.end_time) for item in drafts]
    uncovered = _uncovered_spans(event, part_spans, times)
    if uncovered:
        raise DraftEditError(
            "원래 event 의 근거 중 어느 조각에도 담기지 않는 구간이 있습니다: "
            f"{', '.join(uncovered)}. 그 시간을 덮는 조각을 더해야 합니다."
        )

    assigned = _assign_refs(list(event.source_refs), part_spans, times)
    for index, (item, refs) in enumerate(zip(drafts, assigned), start=1):
        if not refs:
            raise DraftEditError(
                f"조각 {index} 의 시간에 해당하는 근거가 없습니다. 근거가 있는 "
                "시간으로 나눠야 합니다."
            )
        item.source_refs = refs

    position = draft.events.index(event)
    draft.events[position : position + 1] = drafts
    logger.debug(
        "Repair: event 분할 clientEventId=%s, parts=%d", client_event_id, len(drafts)
    )
    return drafts
