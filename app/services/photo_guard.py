"""사진 귀속 검사 (#56 §7.3).

사진은 사용자가 직접 골라 넣은 입력이다. 다른 source 와 달리 "센서가 남긴 흔적" 이
아니라 **사용자가 타임라인에 넣으려고 선택한 것**이라, 최종 결과에서 조용히 사라지면
사용자가 곧바로 알아챈다.

그래서 사진에는 두 가지 계약이 있다.

    - 정상 처리된 사진 rawId 는 최종 event 중 **정확히 하나**에만 들어간다.
    - 하나의 event 에는 같은 사건을 보여 주는 사진이 여럿 들어갈 수 있다(N:1).

App Server 로 나가는 `TimelineResultEvent` 에는 UI 표시 필드가 없다. 사진이 어느 event
에 보이는지는 `sourceRawIds` 포함 여부로만 정해지므로, 위 계약이 곧 "대표 event 지정" 과
"중복 표시 없음" 이다.

**이 계약은 코드가 강제한다(#119).** 예전에는 검출만 하고 어느 event 에 남길지를 Repair 에
맡겼다. 그런데 Timeline 이 설명 없는 사진을 번번이 빠뜨렸고, Repair 가 실패하거나 제한
시간이 끝나면 어긋난 채로 저장됐다. 사용자가 직접 고른 사진이 사라지거나 두 번 보이는
것은 있어서는 안 되는 일이라, 확정할 때마다 `enforce_photo_assignment` 가 항상 그 상태로
만든다. 어떤 입력에서도 실패하지 않는다.

코드가 고르는 event 가 의미까지 맞는다는 보장은 없다. 그래서 Repair 는 사진을 옮길 수
있다 — 같은 계획 안에서 원래 event 에서 빼고 옮길 event 에 넣으면 된다. 한쪽만 하면 다음
확정이 아래 규칙으로 되돌린다.

`verify_photo_assignment`·`inspect_photo_assignment` 는 검출만 한다. 강제 뒤에 남은 위반이
있는지 확인하고 Repair 도구가 상태를 조회하는 데 쓴다.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from app.core.logging import get_logger, log_fields
from app.schemas import (
    AiEventCandidate,
    EventSourceType,
    EventType,
    InferenceLevel,
    PhotoItem,
    SourceRef,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.location_link import LINK_REASON
from app.services.location_metrics import same_photo_scene
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

#: warning 문구에 실을 rawId 예시 개수. 전부 나열하면 사용자에게 의미 없는 긴 목록이 된다.
_SAMPLE_LIMIT = 3

#: 코드가 만든 사진 event 의 confidence. 사진이 찍혔다는 것만 확실하고 무엇을 찍었는지는
#: 코드가 모른다.
_CREATED_CONFIDENCE = 0.4

#: 코드가 만든 사진 event 의 문장. candidate 에서 만들어도 이 틀을 쓴다(#163) — candidate
#: 문장은 Event Agent 의 보고라 분 단위 시각·추정 표현·종결형 제목이 들어 있어 사용자에게
#: 그대로 보일 수 없다. Repair 가 돌면 그 candidate 를 보고 다시 쓴다.
CREATED_PHOTO_TITLE = "사진으로 남긴 순간"
_CREATED_DESCRIPTION = "사진을 남겼다."
_CREATED_NOTE = (
    "사진을 담을 event 가 없어 촬영 시각으로 만든 기록이다. 사진 내용은 확인하지 못했다."
)
_RESTORED_NOTE = (
    "어느 event 에도 쓰이지 않은 사진 candidate 로 만든 기록이다. 무엇을 했는지는 그 "
    "candidate 에 있다."
)

_REASON_CONTAINS = "촬영 시각이 이 event 안에 있다."
_REASON_CREATED = "사진을 담을 event 가 없어 촬영 시각으로 기록했다."
_REASON_RESTORED = "어느 event 에도 쓰이지 않은 사진 candidate 의 사진이다."


@dataclass
class PhotoAssignment:
    """사진 rawId 가 최종 draft 에서 어떻게 귀속됐는지."""

    #: 입력으로 들어온 사진 rawId 전체.
    input_raw_ids: set[str] = field(default_factory=set)
    #: rawId → 그 사진을 근거로 쓴 event 의 clientEventId 목록.
    event_ids_by_raw_id: dict[str, list[str]] = field(default_factory=dict)

    @property
    def missing(self) -> set[str]:
        """어느 event 에도 들어가지 못한 사진."""

        return {
            raw_id
            for raw_id in self.input_raw_ids
            if not self.event_ids_by_raw_id.get(raw_id)
        }

    @property
    def duplicated(self) -> dict[str, list[str]]:
        """둘 이상의 event 에 들어간 사진."""

        return {
            raw_id: event_ids
            for raw_id, event_ids in self.event_ids_by_raw_id.items()
            if len(event_ids) > 1
        }

    @property
    def assigned_count(self) -> int:
        return sum(1 for ids in self.event_ids_by_raw_id.values() if len(ids) == 1)


def inspect_photo_assignment(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> PhotoAssignment:
    """입력 사진 rawId 와 최종 event `sourceRefs` 를 대조한다(draft 를 바꾸지 않는다)."""

    input_raw_ids = {
        raw_id for item in request.photos if (raw_id := raw_id_of(item)) is not None
    }

    event_ids_by_raw_id: dict[str, list[str]] = {raw_id: [] for raw_id in input_raw_ids}
    for event in draft.events:
        for ref in event.source_refs:
            if ref.source_type is not EventSourceType.PHOTO:
                continue
            raw_id = str(ref.raw_id)
            if raw_id in event_ids_by_raw_id:
                event_ids_by_raw_id[raw_id].append(event.client_event_id)

    return PhotoAssignment(
        input_raw_ids=input_raw_ids, event_ids_by_raw_id=event_ids_by_raw_id
    )


def verify_photo_assignment(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> PhotoAssignment:
    """사진 귀속을 검사하고 문제를 draft warning 으로 남긴다.

    draft 의 event 는 고치지 않는다. 어느 event 가 그 사진의 주인인지는 코드가 정할 수
    없다 — 시각이 가깝다고 의미까지 맞는 것은 아니기 때문이다.
    """

    assignment = inspect_photo_assignment(draft, request)
    if not assignment.input_raw_ids:
        return assignment

    missing = sorted(assignment.missing)
    duplicated = assignment.duplicated

    if missing:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-photo-missing-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.HIGH,
                message=(
                    f"선택한 사진 {len(missing)}장이 타임라인의 어느 event 에도 "
                    "연결되지 않았습니다."
                ),
            )
        )
    if duplicated:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-photo-duplicate-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"사진 {len(duplicated)}장이 여러 event 에 함께 연결됐습니다. "
                    "사진 한 장은 하나의 event 에만 속해야 합니다."
                ),
            )
        )

    if missing or duplicated:
        # rawId 는 운영 이벤트로 나가지 않는다(#53). 로컬 진단으로만 남긴다.
        logger.debug(
            "사진 귀속 문제 검출",
            extra=log_fields(
                photoInputCount=len(assignment.input_raw_ids),
                photoAssignedCount=assignment.assigned_count,
                photoMissingCount=len(missing),
                photoDuplicatedCount=len(duplicated),
                photoMissingSample=missing[:_SAMPLE_LIMIT],
                photoDuplicatedSample=sorted(duplicated)[:_SAMPLE_LIMIT],
            ),
        )

    return assignment


# --- 강제 ----------------------------------------------------------------------


@dataclass
class PhotoEnforcement:
    """`enforce_photo_assignment` 가 실제로 한 일."""

    #: 여러 event 에 걸려 있어 하나만 남긴 사진.
    deduplicated: list[str] = field(default_factory=list)
    #: 어느 event 에도 없어 붙인 사진.
    attached: list[str] = field(default_factory=list)
    #: 담을 event 가 없거나 Timeline 이 버린 사진 candidate 라 새로 만든 event.
    created: list[TimelineEventDraft] = field(default_factory=list)
    #: 사진을 빼고 나니 근거가 하나도 남지 않아 지운 event.
    removed: list[TimelineEventDraft] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(
            self.deduplicated or self.attached or self.created or self.removed
        )

    @property
    def changed_composition(self) -> bool:
        """event 가 생기거나 사라졌는가. 그랬다면 정렬하고 새 event 에 id 를 줘야 한다."""

        return bool(self.created or self.removed)


def _gap(event: TimelineEventDraft, taken: datetime | None) -> timedelta:
    """촬영 시각과 event 사이의 거리. event 안이면 0 이다."""

    if taken is None:
        return timedelta(0)
    if taken < event.start_time:
        return event.start_time - taken
    if taken > event.end_time:
        return taken - event.end_time
    return timedelta(0)


def _by_time(event: TimelineEventDraft, taken: datetime | None):
    """촬영 시각을 포함하는 event → 가까운 event → 짧은 event → 확신이 높은 event."""

    return (
        _gap(event, taken),
        event.end_time - event.start_time,
        -event.confidence,
        event.start_time,
        event.title,
    )


def _is_own_evidence(ref: SourceRef) -> bool:
    """event 가 스스로 댄 근거인가.

    확정 단계가 일정·사진 event 에 장소 근거로 붙인 체류(#138)는 아니다. 그 체류는 사진이
    있어서 붙은 것이라, 사진을 빼면 함께 뜻을 잃는다. 근거로 치면 사진만으로 만든 event 가
    사진을 잃고도 체류 카드로 살아남는다.
    """

    return ref.reason != LINK_REASON


def _has_other_evidence(event: TimelineEventDraft, photo_ids: set[str]) -> bool:
    return any(
        ref.raw_id not in photo_ids and _is_own_evidence(ref) for ref in event.source_refs
    )


def _collect_holders(
    draft: TimelineDraft, photo_ids: set[str]
) -> dict[str, list[TimelineEventDraft]]:
    """사진 rawId → 그 사진을 가진 event 들. 한 event 안의 같은 사진은 하나로 줄인다.

    사진인지는 `sourceType` 라벨이 아니라 rawId 로 판정한다. 라벨은 LLM 이 붙인 값이라
    틀릴 수 있고, 라벨만 보면 `STAY` 라고 적힌 사진 참조가 검사를 빠져나간다.
    """

    holders: dict[str, list[TimelineEventDraft]] = {raw_id: [] for raw_id in photo_ids}
    for event in draft.events:
        seen: set[str] = set()
        kept: list[SourceRef] = []
        for ref in event.source_refs:
            if ref.raw_id in photo_ids:
                if ref.raw_id in seen:
                    continue
                seen.add(ref.raw_id)
                holders[ref.raw_id].append(event)
            kept.append(ref)
        if len(kept) != len(event.source_refs):
            event.source_refs = kept
    return holders


def _created_time(
    taken: datetime | None, request: TimelineDraftRequest, tz: tzinfo
) -> datetime:
    """새로 만드는 event 의 시각. 촬영 시각을 window 안으로 맞춘다."""

    start = parse_datetime(request.window.start, tz) if request.window else None
    end = parse_datetime(request.window.end, tz) if request.window else None

    moment = taken or start or parse_datetime(request.date, tz)
    if moment is None:
        # 날짜조차 읽을 수 없는 요청이다. 앞 단계가 이미 걸렀어야 하지만, 여기서
        # 예외를 던지면 사진 한 장 때문에 하루 전체가 실패한다.
        moment = datetime.now(tz)
    if start is not None and moment < start:
        moment = start
    if end is not None and start is not None and end >= start and moment > end:
        moment = end
    return moment


def _create_event(
    photos: list[PhotoItem],
    start: datetime,
    end: datetime,
    sequence: int,
    *,
    reason: str,
    note: str,
) -> TimelineEventDraft:
    first = photos[0]
    return TimelineEventDraft(
        # 임시 id 다. 확정 마지막이 정식 번호를 준다.
        client_event_id=f"event-photo-{sequence:03d}",
        event_type=EventType.PHOTO_MOMENT,
        title=CREATED_PHOTO_TITLE,
        description=_CREATED_DESCRIPTION,
        place=next((name for name in first.places if name and name.strip()), None),
        address=first.address,
        start_time=start,
        end_time=max(start, end),
        confidence=_CREATED_CONFIDENCE,
        inference_level=InferenceLevel.UNCERTAIN,
        source_refs=[
            SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id, reason=reason)
            for photo in photos
        ],
        uncertainty=[note],
    )


def _dropped_candidates(
    candidates: Sequence[AiEventCandidate], photo_ids: set[str], missing: set[str]
) -> list[tuple[AiEventCandidate, list[str]]]:
    """사진이 하나도 event 에 쓰이지 않은 candidate 와 그 사진 rawId.

    Timeline 이 candidate 를 통째로 버린 경우다. 사진 일부라도 event 에 있으면 Timeline 이
    그 candidate 를 쓴 것이라 고르지 않는다. 사진은 candidate 하나에만 담는다.
    """

    dropped: list[tuple[AiEventCandidate, list[str]]] = []
    claimed: set[str] = set()
    for candidate in candidates:
        raw_ids = list(
            dict.fromkeys(ref.raw_id for ref in candidate.source_refs if ref.raw_id in photo_ids)
        )
        if raw_ids and all(raw_id in missing and raw_id not in claimed for raw_id in raw_ids):
            dropped.append((candidate, raw_ids))
            claimed.update(raw_ids)
    return dropped


def _created_by_code(event: TimelineEventDraft) -> bool:
    """촬영 시각만으로 코드가 만든 사진 event 인가."""

    return event.event_type is EventType.PHOTO_MOMENT and all(
        ref.reason == _REASON_CREATED for ref in event.source_refs
    )


def enforce_photo_assignment(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    candidates: Sequence[AiEventCandidate] = (),
    join_created_within: timedelta | None = None,
    *,
    prefer_extracted: bool = False,
) -> PhotoEnforcement:
    """입력의 모든 사진이 정확히 한 event 에만 있게 만든다(in-place).

    순서대로 적용한다.

        1. 여러 event 에 걸린 사진은 하나만 남긴다. **사진 말고 다른 근거가 있는 event 가
           먼저다.** 체류·일정·알림과 함께 사진을 가진 event 는 그 사진으로 무엇을 했는지
           말하는 event 이고, 사진만으로 만든 event 는 같은 사진을 한 번 더 보여 주는
           카드다. 그다음은 촬영 시각을 포함하는 event, 가장 짧은 event 순이다.
        2. 사진을 빼서 근거가 하나도 남지 않은 event 는 지운다. 근거 없는 event 는 저장 전
           검증을 통과하지 못해 하루 전체가 실패한다. 확정 단계가 장소 근거로 붙인 체류만
           남은 event 도 지운다(#138) — 그 체류는 사진이 있어서 붙은 것이다.
        3. 사진이 하나도 event 에 쓰이지 않은 candidate(Timeline 이 통째로 버린 사진
           candidate)는 그 사진들을 담은 `PHOTO_MOMENT` 하나로 만든다. 시간은 candidate 의
           시간이다(#163).
        4. 남은 사진은 촬영 시각을 포함하는 event 에 붙이고, 그런 event 가 없으면 촬영
           시각의 `PHOTO_MOMENT` 를 만들어 담는다.

    `join_created_within` 이 있으면 4 에서 새로 만드는 사진 event 를 이어진 사진끼리 하나로
    만든다 — 직전에 만든 사진 event 가 끝난 뒤 이 시간 안에 찍힌 사진은, 그 event 의 사진과
    같은 장면(`same_photo_scene`, 같은 곳)일 때 그 event 에 담는다. 시각만 보면 10분 간격으로
    11km 떨어진 사진이 한 event 가 됐다.
    v4 세트는 사진 candidate 가 없어 3 이 돌지 않으므로, 같은 장면을 연달아 찍은 사진이 카드
    여러 장이 되지 않게 이것으로 묶는다.

    `prefer_extracted`(v4)이면 1 의 "다른 근거가 있는 event 먼저" 를 쓰지 않고 촬영 시각을 품는
    가장 짧은 event 에 남긴다 — 그 사진으로 꺼낸 event 다(사용자 결정 2026-10-30, "꺼냈으면 원래거는
    지워야지"). Timeline 이 음식 사진으로 식사를 꺼내고 같은 사진을 회의 근거에도 남기자, 다른 근거가
    있는 회의가 사진을 가져가 사진뿐이던 식사가 지워졌다.

    **가장 가까운 event 에는 붙이지 않는다(#163).** 시각이 가깝다고 같은 사건이 아니다.
    22:56 저녁 사진이 22:01 에 끝난 대화에 붙어 저녁이 하루에서 사라졌고, Repair 는 그
    사진을 떼려다 실패했다.

    두 번 적용해도 결과가 같다.
    """

    enforcement = PhotoEnforcement()
    photos = {
        raw_id: item for item in request.photos if (raw_id := raw_id_of(item)) is not None
    }
    if not photos:
        return enforcement

    tz = resolve_timezone(request.timezone)
    photo_ids = set(photos)
    taken_at = {
        raw_id: parse_datetime(item.taken_at, tz) for raw_id, item in photos.items()
    }
    holders = _collect_holders(draft, photo_ids)

    for raw_id in sorted(photo_ids):
        owners = holders[raw_id]
        if len(owners) < 2:
            continue
        taken = taken_at[raw_id]
        keeper = min(
            owners,
            key=lambda event: (
                False if prefer_extracted else not _has_other_evidence(event, photo_ids),
                *_by_time(event, taken),
            ),
        )
        for event in owners:
            if event is keeper:
                continue
            event.source_refs = [
                ref for ref in event.source_refs if ref.raw_id != raw_id
            ]
        enforcement.deduplicated.append(raw_id)

    emptied = [
        event
        for event in draft.events
        if not any(_is_own_evidence(ref) for ref in event.source_refs)
    ]
    if emptied:
        gone = {id(event) for event in emptied}
        draft.events = [event for event in draft.events if id(event) not in gone]
        enforcement.removed.extend(emptied)

    missing = {raw_id for raw_id in photo_ids if not holders[raw_id]}
    for candidate, raw_ids in _dropped_candidates(candidates, photo_ids, missing):
        created = _create_event(
            [photos[raw_id] for raw_id in raw_ids],
            _created_time(candidate.time_range.start_time, request, tz),
            _created_time(candidate.time_range.end_time, request, tz),
            len(enforcement.created) + 1,
            reason=_REASON_RESTORED,
            note=_RESTORED_NOTE,
        )
        draft.events.append(created)
        enforcement.created.append(created)
        missing.difference_update(raw_ids)

    order = sorted(missing)
    if join_created_within is not None:
        # 이어진 사진끼리 묶으려면 촬영 순서대로 돌아야 한다.
        order.sort(key=lambda r: (taken_at[r] is None, taken_at[r].timestamp() if taken_at[r] else 0))
    for raw_id in order:
        taken = taken_at[raw_id]
        containing = (
            [event for event in draft.events if _gap(event, taken) == timedelta(0)]
            if taken is not None
            else []
        )
        if containing:
            target = min(containing, key=lambda event: _by_time(event, taken))
            target.source_refs = [
                *target.source_refs,
                SourceRef(
                    source_type=EventSourceType.PHOTO,
                    raw_id=raw_id,
                    reason=_REASON_CONTAINS,
                ),
            ]
            enforcement.attached.append(raw_id)
            continue
        moment = _created_time(taken, request, tz)
        # 앞선 확정에서 만든 사진 event 도 묶는 대상이다 — 사진이 확정마다 하나씩 빠질 수 있다.
        previous = (
            next(
                (
                    event
                    for event in draft.events
                    if _created_by_code(event)
                    and event.start_time - join_created_within
                    <= moment
                    <= event.end_time + join_created_within
                    and _same_scene_as_any(raw_id, event, photos, taken_at)
                ),
                None,
            )
            if join_created_within is not None
            else None
        )
        if previous is not None:
            previous.source_refs = [
                *previous.source_refs,
                SourceRef(source_type=EventSourceType.PHOTO, raw_id=raw_id, reason=_REASON_CREATED),
            ]
            previous.start_time = min(previous.start_time, moment)
            previous.end_time = max(previous.end_time, moment)
            continue
        created = _create_event(
            [photos[raw_id]],
            moment,
            moment,
            len(enforcement.created) + 1,
            reason=_REASON_CREATED,
            note=_CREATED_NOTE,
        )
        draft.events.append(created)
        enforcement.created.append(created)

    if not enforcement.acted:
        return enforcement

    if enforcement.deduplicated or enforcement.removed:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-photo-enforced-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"여러 event 에 걸린 사진 {len(enforcement.deduplicated)}장을 한 "
                    f"event 에만 남겼습니다. 근거가 남지 않은 event "
                    f"{len(enforcement.removed)}건은 제외했습니다."
                ),
            )
        )
    if enforcement.attached or enforcement.created:
        placed = len(enforcement.attached) + sum(
            len(event.source_refs) for event in enforcement.created
        )
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-photo-enforced-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"어느 event 에도 없던 사진 {placed}장을 촬영 시각을 품는 event 에 "
                    "붙이거나 사진 event 로 새로 담았습니다."
                ),
            )
        )

    # rawId 는 운영 이벤트로 나가지 않는다(#53). 건수만 남긴다.
    logger.debug(
        "사진 단일 귀속 강제",
        extra=log_fields(
            photoInputCount=len(photo_ids),
            photoDeduplicatedCount=len(enforcement.deduplicated),
            photoAttachedCount=len(enforcement.attached),
            photoCreatedEventCount=len(enforcement.created),
            photoRemovedEventCount=len(enforcement.removed),
        ),
    )
    return enforcement


def _same_scene_as_any(raw_id: str, event: TimelineEventDraft, photos, taken_at) -> bool:
    """사진이 event 에 담긴 사진 중 하나와 같은 장면인가."""

    item = photos[raw_id]
    return any(
        same_photo_scene(
            taken_at.get(ref.raw_id),
            (other.latitude, other.longitude),
            taken_at[raw_id],
            (item.latitude, item.longitude),
        )
        for ref in event.source_refs
        if (other := photos.get(ref.raw_id)) is not None
    )


def merge_photo_scenes(draft: TimelineDraft, request: TimelineDraftRequest) -> list[str]:
    """같은 장면 사진이 여러 event 에 나뉘어 있으면 한 event 로 모은다(in-place, v4).

    같은 장면은 `location_metrics.same_photo_scene` 이 정한다. 사진을 가장 많이 가진 event 가
    모으는 쪽이고, 같으면 사진 순간이 아닌 event(식사처럼 사진이 말하는 활동), 짧은 event(그 사진으로
    꺼낸 event — 꺼냈으면 원래 event 에서는 뺀다, 사용자 결정 2026-10-30), 먼저 시작한 event 순이다.
    Timeline 이 같은 장면(12:49)에서 점심과 사진 순간을 함께 꺼냈을 때 짧은 사진 순간이 사진을 모두
    가져가 점심이 사진 없는 식사가 됐다. 다른 event
    에서는 그 사진을 빼고, 근거가 남지 않으면 지운다. 모은 event 의 시간은 장면의 촬영 시각을 품게
    넓힌다. 모은 사진의 rawId 를 돌려준다.
    """

    tz = resolve_timezone(request.timezone)
    photos = sorted(
        (
            (taken, raw_id, (item.latitude, item.longitude))
            for item in request.photos
            if (raw_id := raw_id_of(item)) is not None
            and (taken := parse_datetime(item.taken_at, tz)) is not None
        ),
        key=lambda photo: photo[0],
    )
    scenes: list[list[tuple[datetime, str, tuple]]] = []
    for photo in photos:
        last = scenes[-1][-1] if scenes else None
        if last is not None and same_photo_scene(last[0], last[2], photo[0], photo[2]):
            scenes[-1].append(photo)
        else:
            scenes.append([photo])

    moved: list[str] = []
    photo_ids = {raw_id for _, raw_id, _ in photos}
    for scene in scenes:
        scene_ids = {raw_id for _, raw_id, _ in scene}
        holders = [
            event
            for event in draft.events
            if any(ref.raw_id in scene_ids for ref in event.source_refs)
        ]
        if len(scene) < 2 or len(holders) < 2:
            continue
        target = max(
            holders,
            key=lambda event: (
                sum(ref.raw_id in scene_ids for ref in event.source_refs),
                event.event_type is not EventType.PHOTO_MOMENT,
                -(event.end_time - event.start_time).total_seconds(),
                -event.start_time.timestamp(),
            ),
        )
        held = {ref.raw_id for ref in target.source_refs}
        for event in holders:
            if event is target:
                continue
            taken = [ref for ref in event.source_refs if ref.raw_id in scene_ids]
            event.source_refs = [ref for ref in event.source_refs if ref.raw_id not in scene_ids]
            for ref in taken:
                if ref.raw_id not in held:
                    target.source_refs = [*target.source_refs, ref]
                    held.add(ref.raw_id)
                    moved.append(ref.raw_id)
        times = [taken for taken, raw_id, _ in scene]
        target.start_time = min(target.start_time, *times)
        target.end_time = max(target.end_time, *times)
        draft.events = [
            event
            for event in draft.events
            if event is target or any(_is_own_evidence(ref) for ref in event.source_refs)
        ]
    if moved:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-photo-scene-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=f"같은 장면 사진 {len(moved)}장이 다른 event 에 나뉘어 있어 한 event 로 모았습니다.",
            )
        )
    return moved


def enforce_photo_scenes(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """v4 의 사진 단일 귀속: 단일 귀속을 강제하고, 되살린 사진은 이어 찍은 것끼리 묶고, 같은 장면은
    한 event 로 모은다."""

    from app.services.location_metrics import SHORT_STAY_MAX

    enforce_photo_assignment(
        draft, request, join_created_within=SHORT_STAY_MAX, prefer_extracted=True
    )
    merge_photo_scenes(draft, request)
