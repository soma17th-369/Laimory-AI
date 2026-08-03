"""candidate·fragment 보존 검사 (#56 §8.5).

Fragment 는 "독립 candidate 를 만들기엔 근거가 약하지만 버리기엔 아까운 유효 raw item"
을 보존하는 자리다. 이 개념이 v2 에서 모든 Event Agent 에 통일됐고, 그에 따라 두 가지가
계약이 됐다.

    - Agent 에 전달된 유효 raw item 은 candidate 또는 fragment 중 한 곳에 남는다.
    - 같은 rawId 가 candidate 와 fragment에 동시에 들어가지 않는다.
    - Timeline 이 fragment 를 근거로 썼으면 그 rawId 가 최종 event `sourceRefs` 에 남는다.

이 모듈은 그 계약이 지켜졌는지 대조한다. 고치지 않는다 — 어느 fragment 가 어느 event 의
근거였는지는 의미 판단이라 코드가 되돌릴 수 없다. 대신 무엇이 사라졌는지 알려 준다.

`source_integrity` 와 역할이 다르다. 그쪽은 **입력에 없는 rawId** 를 지우고, 이쪽은
**입력에 있는데 결과에 없는 rawId** 를 찾는다. 방향이 반대다.
"""

from dataclasses import dataclass, field

from app.core.logging import get_logger, log_fields
from app.schemas import (
    AgentEventResult,
    AgentWarning,
    TimelineDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)

logger = get_logger(__name__)


@dataclass
class FragmentCoverage:
    """Agent 결과에서 raw item 이 어떻게 보존됐는지."""

    candidate_raw_ids: set[str] = field(default_factory=set)
    fragment_raw_ids: set[str] = field(default_factory=set)
    #: candidate 에도 fragment 에도 없는 입력 rawId.
    dropped: set[str] = field(default_factory=set)
    #: candidate 와 fragment 양쪽에 들어간 rawId.
    duplicated: set[str] = field(default_factory=set)


def inspect_agent_coverage(
    result: AgentEventResult, input_raw_ids: set[str]
) -> FragmentCoverage:
    """Agent 결과가 입력 raw item 을 얼마나 보존했는지 계산한다."""

    candidate_raw_ids = {
        str(ref.raw_id)
        for candidate in result.candidates
        for ref in candidate.source_refs
    }
    fragment_raw_ids = {str(fragment.raw_id) for fragment in result.fragments}
    covered = candidate_raw_ids | fragment_raw_ids

    return FragmentCoverage(
        candidate_raw_ids=candidate_raw_ids,
        fragment_raw_ids=fragment_raw_ids,
        dropped=input_raw_ids - covered,
        duplicated=candidate_raw_ids & fragment_raw_ids,
    )


def verify_agent_coverage(
    result: AgentEventResult,
    input_raw_ids: set[str],
    *,
    agent_name: str,
) -> FragmentCoverage:
    """보존 계약을 검사하고 어긋나면 Agent warning 을 붙인다."""

    coverage = inspect_agent_coverage(result, input_raw_ids)

    if coverage.dropped:
        result.warnings.append(
            AgentWarning(
                agent_name=agent_name,
                message=(
                    f"입력 {len(coverage.dropped)}건이 후보에도 단서에도 남지 않았습니다. "
                    "독립 후보로 세우기 어려운 항목은 단서로 보존해야 합니다."
                ),
            )
        )
    if coverage.duplicated:
        result.warnings.append(
            AgentWarning(
                agent_name=agent_name,
                message=(
                    f"{len(coverage.duplicated)}건이 후보와 단서 양쪽에 함께 들어갔습니다. "
                    "각 항목은 같은 의미의 후보와 단서 중 한 곳에만 있어야 합니다."
                ),
            )
        )

    if coverage.dropped or coverage.duplicated:
        logger.debug(
            "raw item 보존 계약 위반",
            extra=log_fields(
                agent=agent_name,
                inputCount=len(input_raw_ids),
                droppedCount=len(coverage.dropped),
                duplicatedCount=len(coverage.duplicated),
            ),
        )
    return coverage


def verify_fragment_usage(
    draft: TimelineDraft, fragment_raw_ids: set[str]
) -> None:
    """fragment 만으로 세워진 event 를 Repair 검토 대상으로 표시한다.

    fragment 는 가장 낮은 우선순위의 근거다. 오직 fragment 만 근거인 event 가 하루의
    중심에 놓이면 근거보다 이야기가 앞선 것이므로 사람이 한 번 봐야 한다.
    """

    if not fragment_raw_ids:
        return

    fragment_only = [
        event
        for event in draft.events
        if event.source_refs
        and all(str(ref.raw_id) in fragment_raw_ids for ref in event.source_refs)
    ]
    if not fragment_only:
        return

    draft.warnings.append(
        TimelineWarning(
            warning_id=f"warning-fragment-only-{len(draft.warnings) + 1:03d}",
            severity=TimelineWarningSeverity.MEDIUM,
            message=(
                f"event {len(fragment_only)}건이 낮은 우선순위 단서만을 근거로 만들어졌습니다. "
                "근거가 충분한지 확인이 필요합니다."
            ),
        )
    )
    logger.debug(
        "fragment 단독 근거 event",
        extra=log_fields(fragmentOnlyEventCount=len(fragment_only)),
    )
