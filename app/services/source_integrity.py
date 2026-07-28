"""Agent 결과의 rawId 참조 무결성을 강제한다.

정규화된 요청의 source 항목이 가진 ``rawId``만 유효한 근거다. LLM이 입력에 없는
rawId를 만들면 해당 참조를 제거하고, 유효한 근거가 하나도 남지 않은 후보·event는
다음 단계로 넘기지 않는다. 내부 DB ID를 source 식별자로 대체하는 fallback은 없다.
"""

from dataclasses import dataclass

from app.schemas import (
    AgentEventResult,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)


@dataclass(frozen=True)
class SourceFilterStats:
    """rawId 정리 결과."""

    removed_refs: int = 0
    dropped_items: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.removed_refs or self.dropped_items)

    def observation_payload(self, *, item_kind: str) -> dict[str, str | int]:
        """rawId 원문 없이 운영 관측에 남길 안전한 위반 메타데이터."""

        return {
            "validationCode": "SOURCE_RAW_ID_NOT_IN_REQUEST",
            "itemKind": item_kind,
            "removedRefCount": self.removed_refs,
            "droppedItemCount": self.dropped_items,
        }


def request_raw_ids(request: TimelineDraftRequest) -> set[str]:
    """현재 요청에 실제로 존재하는 rawId 집합."""

    return {item.raw_id for item in request.iter_source_items()}


def filter_agent_result_sources(
    result: AgentEventResult,
    request: TimelineDraftRequest,
) -> tuple[AgentEventResult, SourceFilterStats]:
    """Event Agent 결과에서 입력에 없는 rawId 참조를 제거한다."""

    valid_raw_ids = request_raw_ids(request)
    removed_refs = 0
    dropped_items = 0
    candidates = []

    for candidate in result.candidates:
        refs = [ref for ref in candidate.source_refs if ref.raw_id in valid_raw_ids]
        removed_refs += len(candidate.source_refs) - len(refs)
        if not refs:
            dropped_items += 1
            continue
        candidates.append(candidate.model_copy(update={"source_refs": refs}))

    fragments = []
    for fragment in result.fragments:
        if fragment.raw_id not in valid_raw_ids:
            removed_refs += 1
            dropped_items += 1
            continue
        fragments.append(fragment)

    filtered = AgentEventResult(
        candidates=candidates,
        fragments=fragments,
        warnings=list(result.warnings),
    )
    return filtered, SourceFilterStats(removed_refs, dropped_items)


def filter_draft_sources(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
) -> SourceFilterStats:
    """Timeline draft의 환각 rawId를 제거하고 근거 없는 event를 제외한다."""

    valid_raw_ids = request_raw_ids(request)
    removed_refs = 0
    dropped_items = 0
    events = []

    for event in draft.events:
        refs = [ref for ref in event.source_refs if ref.raw_id in valid_raw_ids]
        removed_refs += len(event.source_refs) - len(refs)
        if not refs:
            dropped_items += 1
            continue
        events.append(event.model_copy(update={"source_refs": refs}))
    draft.events = events

    # warning 자체는 근거가 없어도 유효하므로 잘못된 참조만 제거한다.
    for warning in draft.warnings:
        refs = [ref for ref in warning.source_refs if ref.raw_id in valid_raw_ids]
        removed_refs += len(warning.source_refs) - len(refs)
        warning.source_refs = refs

    stats = SourceFilterStats(removed_refs, dropped_items)
    if stats.changed:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-source-integrity-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.HIGH,
                message=(
                    "입력에 없는 rawId 참조 "
                    f"{stats.removed_refs}건을 제거하고, 유효한 근거가 남지 않은 "
                    f"event {stats.dropped_items}건을 제외했습니다."
                ),
            )
        )
    return stats
