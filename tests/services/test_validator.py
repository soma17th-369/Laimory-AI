"""요청 시간 범위(window) 강제 검증.

Event Agent 후보 단계 필터(`filter_result_to_window`)와 최종 draft 검증
(`validate_draft_to_window`)이 window 밖 event 를 제거/경고하는지 확인한다.
"""

from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineQuestion,
    TimelineWarningSeverity,
)
from app.services.validator import (
    filter_result_to_window,
    resolve_window_bounds,
    validate_draft_to_window,
)
from tests.fixtures.requests import make_request

# make_request 기본 window: 2026-06-20T00:00:00 ~ 2026-06-21T00:00:00 (KST)
INSIDE = ("2026-06-20T09:00:00+09:00", "2026-06-20T10:00:00+09:00")
OUTSIDE = ("2026-06-19T09:00:00+09:00", "2026-06-19T10:00:00+09:00")
PARTIAL = ("2026-06-19T23:00:00+09:00", "2026-06-20T01:00:00+09:00")


def _bounds():
    bounds = resolve_window_bounds(make_request())
    assert bounds is not None
    return bounds


def _candidate(start, end) -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.REST,
        time_range=CandidateTimeRange(start_time=start, end_time=end),
        title="t",
        description="d",
        source_refs=[SourceRef(source_type=EventSourceType.STAY, source_id="s1")],
        confidence=0.5,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        uncertainty=[],
    )


def _event(cid, start, end) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=cid,
        event_type=EventType.REST,
        title="t",
        start_time=start,
        end_time=end,
        confidence=0.5,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, source_id="s1")],
    )


def test_no_window_returns_none():
    assert resolve_window_bounds(make_request(window=None)) is None


def test_filter_drops_only_outside_candidates():
    bounds = _bounds()
    result = AgentEventResult(
        candidates=[_candidate(*INSIDE), _candidate(*OUTSIDE), _candidate(*PARTIAL)]
    )
    filtered, dropped = filter_result_to_window(result, bounds)

    assert dropped == 1  # OUTSIDE 만 제거, PARTIAL 은 유지
    assert len(filtered.candidates) == 2


def test_validate_draft_drops_outside_and_renumbers():
    bounds = _bounds()
    draft = TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[_event("event-001", *OUTSIDE), _event("event-002", *INSIDE)],
    )
    validate_draft_to_window(draft, bounds)

    assert len(draft.events) == 1
    assert draft.events[0].client_event_id == "event-001"  # 재번호 부여
    assert any(
        w.severity is TimelineWarningSeverity.HIGH and "범위" in w.message
        for w in draft.warnings
    )


def test_validate_draft_flags_partial_but_keeps():
    bounds = _bounds()
    draft = TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[_event("event-001", *PARTIAL)],
    )
    validate_draft_to_window(draft, bounds)

    assert len(draft.events) == 1  # 경계 걸침은 유지
    assert any("경계" in w.message for w in draft.warnings)


def test_related_event_ids_remapped_after_drop():
    bounds = _bounds()
    draft = TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[_event("event-001", *OUTSIDE), _event("event-002", *INSIDE)],
        questions=[
            TimelineQuestion(
                question_id="question-001",
                time_range={"startTime": INSIDE[0], "endTime": INSIDE[1]},
                question="q",
                reason="r",
                related_event_ids=["event-001", "event-002"],
            )
        ],
    )
    validate_draft_to_window(draft, bounds)

    # 제거된 event-001 참조는 버려지고, 살아남은 event-002 는 새 id(event-001)로 매핑된다.
    assert draft.questions[0].related_event_ids == ["event-001"]

