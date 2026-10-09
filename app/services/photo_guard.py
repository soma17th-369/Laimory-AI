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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from app.core.logging import get_logger, log_fields
from app.schemas import (
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
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

#: warning 문구에 실을 rawId 예시 개수. 전부 나열하면 사용자에게 의미 없는 긴 목록이 된다.
_SAMPLE_LIMIT = 3

#: 담을 event 가 하나도 없어 코드가 만든 사진 event 의 confidence. 사진이 찍혔다는 것만
#: 확실하고 무엇을 찍었는지는 모른다.
_CREATED_CONFIDENCE = 0.4

_CREATED_TITLE = "사진으로 남긴 순간"
_CREATED_DESCRIPTION = "사진을 남겼어요."
_CREATED_NOTE = (
    "사진을 담을 event 가 없어 촬영 시각으로 만든 기록이다. 사진 내용은 확인하지 못했다."
)

_REASON_CONTAINS = "촬영 시각이 이 event 안에 있다."
_REASON_NEAREST = "촬영 시각에 가장 가까운 event 다."


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
    #: 담을 event 가 없어 새로 만든 event.
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
    photo: PhotoItem,
    taken: datetime | None,
    request: TimelineDraftRequest,
    tz: tzinfo,
    sequence: int,
) -> TimelineEventDraft:
    moment = _created_time(taken, request, tz)
    return TimelineEventDraft(
        # 임시 id 다. 확정 마지막이 정식 번호를 준다.
        client_event_id=f"event-photo-{sequence:03d}",
        event_type=EventType.PHOTO_MOMENT,
        title=_CREATED_TITLE,
        description=_CREATED_DESCRIPTION,
        place=next((name for name in photo.places if name and name.strip()), None),
        address=photo.address,
        start_time=moment,
        end_time=moment,
        confidence=_CREATED_CONFIDENCE,
        inference_level=InferenceLevel.UNCERTAIN,
        source_refs=[
            SourceRef(
                source_type=EventSourceType.PHOTO,
                raw_id=photo.raw_id,
                reason="사진을 담을 event 가 없어 촬영 시각으로 기록했다.",
            )
        ],
        uncertainty=[_CREATED_NOTE],
    )


def enforce_photo_assignment(
    draft: TimelineDraft, request: TimelineDraftRequest
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
        3. 어느 event 에도 없는 사진은 촬영 시각을 포함하는(없으면 가장 가까운) event 에
           붙인다. Timeline 프롬프트가 쓰는 규칙과 같다.
        4. event 가 하나도 없으면 촬영 시각의 `PHOTO_MOMENT` 를 만들어 담는다.

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
                not _has_other_evidence(event, photo_ids),
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

    missing = sorted(raw_id for raw_id in photo_ids if not holders[raw_id])
    if missing and not draft.events:
        for raw_id in missing:
            created = _create_event(
                photos[raw_id],
                taken_at[raw_id],
                request,
                tz,
                len(enforcement.created) + 1,
            )
            draft.events.append(created)
            enforcement.created.append(created)
    else:
        for raw_id in missing:
            taken = taken_at[raw_id]
            target = min(draft.events, key=lambda event: _by_time(event, taken))
            contains = taken is not None and _gap(target, taken) == timedelta(0)
            target.source_refs = [
                *target.source_refs,
                SourceRef(
                    source_type=EventSourceType.PHOTO,
                    raw_id=raw_id,
                    reason=_REASON_CONTAINS if contains else _REASON_NEAREST,
                ),
            ]
            enforcement.attached.append(raw_id)

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
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-photo-enforced-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    "어느 event 에도 없던 사진 "
                    f"{len(enforcement.attached) + len(enforcement.created)}장을 촬영 "
                    "시각 기준으로 event 에 연결했습니다."
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
