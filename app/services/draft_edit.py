"""draft event 수정·삭제·나누기 (결정론).

Repair Agent 가 "이 event 의 이 필드를 이렇게 고쳐라", "이 event 는 지워라", "이 event 를
이 시각에서 나눠라" 라고 말하면 실제 적용은 여기서 한다. LLM 이 draft 전체를 다시 써 내려가게 두지 않는
이유는 그러면 **손대지 않기로 한 event 까지 조용히 바뀌기** 때문이다. 수정은 지정한
event 의 지정한 필드에만 닿고, 나머지 값은 원본 그대로 남는다.

바꿀 수 있는 필드는 `_EDITABLE_FIELDS` 로 한정한다. `clientEventId` 는 편집 대상이
아니다. 그 id 는 repair 파이프라인이 정렬 결과에 맞춰 다시 부여하는 값이라
(`validator.renumber_events`), LLM 이 임의로 바꾸면 같은 계획 안의 다음 도구 호출과
Question Agent 가 가리키는 곳이 어긋난다.

여기서는 **id 를 다시 매기지 않는다.** 한 번의 개선 계획이 여러 도구 호출을 담기
때문이다. 삭제할 때마다 번호를 다시 매기면 같은 계획 안의 다음 호출이 가리키는
`clientEventId` 가 다른 event 를 뜻하게 된다. 번호 재부여는 도구 실행이 모두 끝난 뒤
`repair_draft` 가 한 번에 한다.
"""

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.schemas import (
    SourceRef,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
)
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

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


def delete_event(draft: TimelineDraft, client_event_id: str) -> TimelineEventDraft:
    """event 한 건을 지운다(in-place). 지워진 event 를 돌려준다."""

    event = find_event(draft, client_event_id)
    draft.events.remove(event)

    # 제목은 사용자 콘텐츠라 남기지 않는다. 무엇이 지워졌는지는 Langfuse 의 도구
    # 실행 기록에서 본다.
    logger.debug("Repair: event 삭제 clientEventId=%s", client_event_id)
    return event


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

    조각의 id 는 임시 값이다(`event-003-1`). 확정 pass 가 정렬 후 다시 매긴다.
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
