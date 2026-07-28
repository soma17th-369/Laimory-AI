"""최종 타임라인 저장 전 자체검증 규칙 검증."""

from datetime import datetime, timedelta, timezone

from app.schemas.event_candidate import EventType, InferenceLevel, SourceRef
from app.schemas.timeline import TimelineDraft, TimelineEventDraft
from app.services.timeline_validator import (
    TimelineValidationError,
    TimelineViolationCode,
    ensure_timeline_valid_for_storage,
    validate_timeline_for_storage,
)
from tests.fixtures.requests import fixture_raw_id

_KST = timezone(timedelta(hours=9))


def _event(eid: str, raw_ids: list[str], **kw) -> TimelineEventDraft:
    defaults = dict(
        client_event_id=eid,
        event_type=EventType.PHOTO_MOMENT,
        title="이벤트",
        description="",
        start_time=datetime(2026, 7, 8, 9, 0, tzinfo=_KST),
        end_time=datetime(2026, 7, 8, 10, 0, tzinfo=_KST),
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type="PHOTO", raw_id=fixture_raw_id(raw_id))
            for raw_id in raw_ids
        ],
    )
    defaults.update(kw)
    return TimelineEventDraft(**defaults)


def _draft(events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date="2026-07-08", timezone="Asia/Seoul", events=events
    )


def test_valid_draft_has_no_violations():
    draft = _draft([_event("e1", ["raw-a", "raw-b"]), _event("e2", ["raw-c"])])
    assert validate_timeline_for_storage(
        draft,
        {fixture_raw_id(raw_id) for raw_id in ("raw-a", "raw-b", "raw-c")},
    ) == []


def test_source_not_in_task_is_flagged():
    draft = _draft([_event("e1", ["raw-x"])])
    violations = validate_timeline_for_storage(draft, {fixture_raw_id("raw-a")})
    assert any(fixture_raw_id("raw-x") in v and "task" in v for v in violations)


def test_source_reused_across_events_is_allowed():
    draft = _draft([_event("e1", ["raw-a"]), _event("e2", ["raw-a"])])
    assert validate_timeline_for_storage(draft, {fixture_raw_id("raw-a")}) == []


def test_same_source_within_one_event_is_not_cross_duplicate():
    # 한 이벤트 안에서 같은 rawId 가 두 번 나와도 교차 중복은 아니다.
    draft = _draft([_event("e1", ["raw-a", "raw-a"])])
    assert validate_timeline_for_storage(draft, {fixture_raw_id("raw-a")}) == []


def test_ensure_valid_raises_on_violation():
    draft = _draft([_event("e1", ["raw-x"])])
    try:
        ensure_timeline_valid_for_storage(draft, {fixture_raw_id("raw-a")})
    except TimelineValidationError as exc:
        assert exc.violations
        assert exc.violation_codes == [
            TimelineViolationCode.SOURCE_RAW_ID_NOT_IN_TASK.value
        ]
    else:  # pragma: no cover - 위반이 있으면 위에서 raise 되어야 한다
        raise AssertionError("TimelineValidationError 가 발생해야 합니다")
