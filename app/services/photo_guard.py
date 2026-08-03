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

**자동 해소는 하지 않는다.** 어느 event 에 남길지는 의미 판단이라 Repair Agent 가
`update_event` 로 정한다. 여기서는 rawId 집합 연산으로 검출하고 warning 으로 알린다.
코드는 결정론적 검출, LLM 은 의미 판단이라는 경계를 그대로 따른다.
"""

from dataclasses import dataclass, field

from app.core.logging import get_logger, log_fields
from app.schemas import (
    EventSourceType,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: warning 문구에 실을 rawId 예시 개수. 전부 나열하면 사용자에게 의미 없는 긴 목록이 된다.
_SAMPLE_LIMIT = 3


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
