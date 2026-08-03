"""candidate·fragment 보존 검사 (#56 §8.5).

Fragment 는 "독립 candidate 로 세우기엔 약하지만 버리기엔 아까운 유효 raw item" 의
자리다. 그래서 Agent 에 전달된 항목은 후보나 단서 중 한 곳에 남아야 하고, 양쪽에
동시에 들어가면 안 된다.

`source_integrity` 와 방향이 반대다. 그쪽은 입력에 **없는** rawId 를 지우고,
이쪽은 입력에 **있는데 결과에 없는** rawId 를 찾는다.
"""

from app.schemas import AgentEventResult, TimelineDraft
from app.services.fragment_guard import (
    inspect_agent_coverage,
    verify_agent_coverage,
    verify_fragment_usage,
)
from tests.fixtures.fake_llm import candidate, fragment
from tests.fixtures.requests import fixture_raw_id

STAY_1 = fixture_raw_id("stay-s1")
STAY_2 = fixture_raw_id("stay-s2")


def _result(candidates=None, fragments=None) -> AgentEventResult:
    return AgentEventResult.model_validate(
        {"candidates": candidates or [], "fragments": fragments or []}
    )


def _messages(result: AgentEventResult) -> str:
    return " ".join(warning.message for warning in result.warnings)


def test_item_kept_as_candidate_is_covered():
    result = _result(candidates=[candidate("REST", [("STAY", "stay-s1")])])

    verify_agent_coverage(result, {STAY_1}, agent_name="location")

    assert result.warnings == []


def test_item_kept_as_fragment_is_covered():
    """단서로만 남아도 보존이다. 그러라고 있는 자리다."""

    result = _result(fragments=[fragment("STAY", STAY_1, "약한 위치 단서")])

    verify_agent_coverage(result, {STAY_1}, agent_name="location")

    assert result.warnings == []


def test_dropped_item_is_warned():
    result = _result(candidates=[candidate("REST", [("STAY", "stay-s1")])])

    coverage = verify_agent_coverage(result, {STAY_1, STAY_2}, agent_name="location")

    assert coverage.dropped == {STAY_2}
    assert "후보에도 단서에도 남지 않았습니다" in _messages(result)


def test_item_in_both_candidate_and_fragment_is_warned():
    result = _result(
        candidates=[candidate("REST", [("STAY", "stay-s1")])],
        fragments=[fragment("STAY", STAY_1, "같은 항목")],
    )

    coverage = verify_agent_coverage(result, {STAY_1}, agent_name="location")

    assert coverage.duplicated == {STAY_1}
    assert "양쪽에 함께 들어갔습니다" in _messages(result)


def test_inspect_does_not_mutate_result():
    result = _result(candidates=[candidate("REST", [("STAY", "stay-s1")])])

    inspect_agent_coverage(result, {STAY_1, STAY_2})

    assert result.warnings == []


def _draft(events: list[dict]) -> TimelineDraft:
    return TimelineDraft.model_validate(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": events,
            "questions": [],
            "warnings": [],
        }
    )


def _event(client_event_id: str, refs: list[tuple[str, str]]) -> dict:
    return {
        "clientEventId": client_event_id,
        "eventType": "REST",
        "title": "제목",
        "description": "",
        "startTime": "2026-06-20T12:00:00+09:00",
        "endTime": "2026-06-20T13:00:00+09:00",
        "confidence": 0.5,
        "inferenceLevel": "INFERRED",
        "sourceRefs": [
            {"sourceType": st, "rawId": raw_id} for st, raw_id in refs
        ],
        "uncertainty": [],
    }


def test_event_built_only_from_fragments_is_flagged():
    """fragment 는 가장 낮은 우선순위 근거다. 그것만으로 선 event 는 사람이 봐야 한다."""

    draft = _draft([_event("event-001", [("STAY", STAY_1)])])

    verify_fragment_usage(draft, {STAY_1})

    assert len(draft.warnings) == 1
    assert "낮은 우선순위 단서만을 근거로" in draft.warnings[0].message


def test_event_with_a_candidate_backed_ref_is_not_flagged():
    draft = _draft([_event("event-001", [("STAY", STAY_1), ("STAY", STAY_2)])])

    verify_fragment_usage(draft, {STAY_1})

    assert draft.warnings == []


def test_no_fragments_is_a_no_op():
    draft = _draft([_event("event-001", [("STAY", STAY_1)])])

    verify_fragment_usage(draft, set())

    assert draft.warnings == []
