"""사용자 노출 문장 길이 검사 (#61).

프롬프트가 모델에게 주는 목표는 100자 내외이고, 이 guard 는 여유를 둬 **120자 초과**만
경고한다. 문체와 문장 수는 의미 판단이라 여기서 재지 않는다 — Timeline·Repair Agent 몫이다.

Repair 는 문장을 여러 번 다시 쓴다. 그래서 재실행 때마다 자기 이전 warning 을 지우고
현재 draft 로 다시 계산하는 것이 계약이다. 안 그러면 Repair 가 문장을 줄인 뒤에도
옛 경고가 남아 "아직 길다" 로 읽힌다.
"""

from app.schemas import TimelineDraft, TimelineWarningSeverity
from app.services.narrative_guard import (
    DESCRIPTION_WARNING_LENGTH,
    verify_narrative_length,
)
from tests.fixtures.requests import fixture_raw_id

STAY_1 = fixture_raw_id("stay-1")


def _draft(events: list[dict], warnings: list[dict] | None = None) -> TimelineDraft:
    return TimelineDraft.model_validate(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": events,
            "questions": [],
            "warnings": warnings or [],
        }
    )


def _event(description: str, *, title: str = "공덕 카페에서 작업") -> dict:
    return {
        "clientEventId": "event-001",
        "eventType": "WORK",
        "title": title,
        "description": description,
        "startTime": "2026-06-20T13:00:00+09:00",
        "endTime": "2026-06-20T15:00:00+09:00",
        "confidence": 0.8,
        "inferenceLevel": "EVIDENCE_BASED",
        "sourceRefs": [{"sourceType": "STAY", "rawId": STAY_1, "reason": "체류"}],
    }


def _narrative_warnings(draft: TimelineDraft) -> list:
    return [w for w in draft.warnings if w.warning_id.startswith("warning-narrative-length-")]


def test_description_at_limit_is_not_warned():
    # 정확히 기준값인 문장은 통과해야 한다. 초과일 때만 경고다.
    draft = _draft([_event("가" * DESCRIPTION_WARNING_LENGTH)])

    verify_narrative_length(draft)

    assert _narrative_warnings(draft) == []


def test_description_over_limit_is_warned():
    draft = _draft([_event("가" * (DESCRIPTION_WARNING_LENGTH + 1))])

    verify_narrative_length(draft)

    warnings = _narrative_warnings(draft)
    assert len(warnings) == 1
    assert warnings[0].severity is TimelineWarningSeverity.LOW
    assert str(DESCRIPTION_WARNING_LENGTH + 1) in warnings[0].message
    assert "공덕 카페에서 작업" in warnings[0].message
    # 어떤 근거에서 나온 문장인지 따라갈 수 있어야 한다.
    assert [ref.raw_id for ref in warnings[0].source_refs] == [STAY_1]


def test_surrounding_whitespace_is_not_counted():
    # 앞뒤 공백 때문에 경고가 뜨면 실제 문장 길이를 잘못 알린다.
    padded = "  " + "가" * DESCRIPTION_WARNING_LENGTH + "\n"
    draft = _draft([_event(padded)])

    verify_narrative_length(draft)

    assert _narrative_warnings(draft) == []


def test_empty_description_is_not_warned():
    draft = _draft([_event("")])

    verify_narrative_length(draft)

    assert _narrative_warnings(draft) == []


def test_repeated_runs_do_not_accumulate():
    draft = _draft([_event("가" * (DESCRIPTION_WARNING_LENGTH + 1))])

    verify_narrative_length(draft)
    verify_narrative_length(draft)

    assert len(_narrative_warnings(draft)) == 1


def test_warning_disappears_after_repair_shortens_sentence():
    draft = _draft([_event("가" * (DESCRIPTION_WARNING_LENGTH + 1))])
    verify_narrative_length(draft)
    assert _narrative_warnings(draft)

    # Repair 가 update_event 로 문장을 줄인 상황.
    draft.events[0].description = "오후에는 공덕 카페에서 커피를 마시며 작업했어요."
    verify_narrative_length(draft)

    assert _narrative_warnings(draft) == []


def test_other_warnings_are_preserved():
    existing = {
        "warningId": "warning-photo-001",
        "severity": "MEDIUM",
        "message": "사진 귀속 문제",
        "sourceRefs": [],
    }
    draft = _draft([_event("가" * (DESCRIPTION_WARNING_LENGTH + 1))], [existing])

    verify_narrative_length(draft)

    assert "warning-photo-001" in {w.warning_id for w in draft.warnings}
