"""최종 타임라인 DB 저장 전 자체검증.

``timeline_events``와 ``timeline_items``에 저장하기 전에 각 이벤트의 필수값과
source 소속을 확인한다. 같은 source가 여러 이벤트의 근거가 되는 것은 허용한다.
향후 source↔timeline item N:M 연결 테이블이 추가돼도 이 계약은 그대로 유지한다.
"""

from app.schemas.timeline import TimelineDraft


class TimelineValidationError(Exception):
    """최종 타임라인 저장 계약 위반."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("타임라인 저장 전 검증 실패: " + "; ".join(violations))


def validate_timeline_for_storage(
    draft: TimelineDraft, valid_raw_ids: set[str]
) -> list[str]:
    """저장 위반 사유를 반환한다. 빈 리스트면 저장할 수 있다."""

    violations: list[str] = []
    for event in draft.events:
        event_id = event.client_event_id

        if not (event.title or "").strip():
            violations.append(f"이벤트 {event_id}: title 이 비어 있습니다")
        if event.start_time is None:
            violations.append(f"이벤트 {event_id}: startTime 이 없습니다")
        if (
            event.end_time is not None
            and event.start_time is not None
            and event.end_time < event.start_time
        ):
            violations.append(f"이벤트 {event_id}: endTime 이 startTime 보다 빠릅니다")

        if not event.source_refs:
            violations.append(f"이벤트 {event_id}: source 가 없습니다")
            continue

        for raw_id in {ref.source_id for ref in event.source_refs}:
            if raw_id not in valid_raw_ids:
                violations.append(
                    f"이벤트 {event_id}: source rawId={raw_id} 가 현재 task 입력에 없습니다"
                )

    return violations


def ensure_timeline_valid_for_storage(
    draft: TimelineDraft, valid_raw_ids: set[str]
) -> None:
    """저장 계약을 검증하고 위반 시 ``TimelineValidationError``를 던진다."""

    violations = validate_timeline_for_storage(draft, valid_raw_ids)
    if violations:
        raise TimelineValidationError(violations)
