"""요청 시간 범위(window) 강제 검증.

Event Agent 후보 단계 필터(`filter_result_to_window`)와 최종 draft 검증
(`validate_draft_to_window`)이 window 밖 event 를 제거/경고하는지 확인한다.
"""

import json
import logging

from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimeWindow,
    TimelineEventDraft,
    TimelineQuestion,
    TimelineWarningSeverity,
)
from app.services.validator import (
    filter_result_to_window,
    resolve_window_bounds,
    validate_draft_to_window,
)
from tests.fixtures.requests import fixture_raw_id, make_request

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
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id("s1"),
            )
        ],
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
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id("s1"),
            )
        ],
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



# --- 저하 이벤트 (이슈 #101) -------------------------------------------------


def _degraded(caplog) -> list[dict]:
    return [
        payload
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
        and payload["event.action"] == "app.degraded"
    ]


def _degraded_levels(caplog) -> set[str]:
    """저하는 실패지만 작업을 죽이지 않는다. ERROR 로 올리면 실제 실패와 섞인다."""

    return {
        record.levelname
        for record in caplog.records
        if (payload := getattr(record, "operational_event", None)) is not None
        and payload["event.action"] == "app.degraded"
    }


def test_unparsable_window_reports_that_range_validation_was_skipped(caplog):
    """범위 검증이 통째로 빠져도 작업은 성공으로 끝난다.

    그래서 이 이벤트가 없으면 window 밖 event 가 섞인 결과와 정상 결과를 운영에서
    구분할 방법이 없다. `component` 는 정규화 실패(`REQUEST`)와 갈리도록 `window` 다.
    """

    with caplog.at_level(logging.DEBUG):
        bounds = resolve_window_bounds(
            make_request(window=TimeWindow(start="깨진값", end="깨진값"))
        )

    assert bounds is None
    events = _degraded(caplog)
    assert len(events) == 1
    assert events[0]["component"] == "window"
    assert _degraded_levels(caplog) == {"WARNING"}
    # window 원문은 요청이 준 값이라 이벤트에도 진단 줄에도 싣지 않는다.
    assert "깨진값" not in json.dumps(events[0], ensure_ascii=False)


def test_reversed_window_also_reports_the_skip(caplog):
    with caplog.at_level(logging.DEBUG):
        bounds = resolve_window_bounds(
            make_request(
                window=TimeWindow(
                    start="2026-06-20T10:00:00+09:00",
                    end="2026-06-20T09:00:00+09:00",
                )
            )
        )

    assert bounds is None
    assert len(_degraded(caplog)) == 1


def test_valid_window_stays_silent(caplog):
    """정상 작업은 저하 이벤트를 하나도 내지 않는다."""

    with caplog.at_level(logging.DEBUG):
        assert resolve_window_bounds(make_request()) is not None

    assert _degraded(caplog) == []
