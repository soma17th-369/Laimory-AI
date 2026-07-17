"""draft event 수정·삭제(결정론) 검증.

Repair Agent 가 "이 event 를 이렇게 고쳐라" 라고 말했을 때 실제로 무엇이 바뀌고 무엇이
바뀌지 않는지를 본다. 핵심은 **지정한 필드에만 닿는다**는 것과, 잘못된 수정을 반쯤
적용하지 않는다는 것이다.
"""

import pytest

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineQuestion,
)
from app.services.draft_edit import DraftEditError, delete_event, find_event, update_event


def _event(client_event_id: str, title: str = "체류", start: str = "09:00", end: str = "10:00"):
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=EventType.REST,
        title=title,
        description="설명",
        place_label="카페",
        tags=["휴식"],
        start_time=f"2026-06-20T{start}:00+09:00",
        end_time=f"2026-06-20T{end}:00+09:00",
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, source_id="s-1")],
    )


def _draft(*events, questions=None) -> TimelineDraft:
    return TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=list(events),
        questions=list(questions or []),
    )


def _question(*related_ids) -> TimelineQuestion:
    return TimelineQuestion(
        question_id="question-001",
        time_range={
            "startTime": "2026-06-20T09:00:00+09:00",
            "endTime": "2026-06-20T10:00:00+09:00",
        },
        question="이 시간에 무엇을 했나요?",
        reason="근거가 약하다",
        related_event_ids=list(related_ids),
    )


def test_update_event_touches_only_given_fields():
    draft = _draft(_event("event-001"))

    updated = update_event(draft, "event-001", {"endTime": "2026-06-20T14:00:00+09:00"})

    assert updated.end_time.hour == 14
    # 지정하지 않은 필드는 원래 값 그대로다.
    assert updated.title == "체류"
    assert updated.place_label == "카페"
    assert updated.tags == ["휴식"]
    assert [ref.source_id for ref in updated.source_refs] == ["s-1"]


def test_update_event_keeps_client_event_id():
    draft = _draft(_event("event-001"))

    # id 는 정렬 결과에 맞춰 코드가 부여하는 값이라 LLM 이 바꿀 수 없다.
    updated = update_event(draft, "event-001", {"title": "카페에서 쉬었다"})

    assert updated.client_event_id == "event-001"
    assert draft.events[0].client_event_id == "event-001"


def test_update_event_rejects_unknown_field():
    draft = _draft(_event("event-001"))

    with pytest.raises(DraftEditError, match="바꿀 수 없는 필드"):
        update_event(draft, "event-001", {"eventId": "e-9"})


def test_update_event_rejects_invalid_value_without_touching_event():
    draft = _draft(_event("event-001"))

    # endTime < startTime 은 스키마가 막는다. 막힌 수정은 event 를 건드리지 않는다.
    with pytest.raises(DraftEditError, match="스키마 검증"):
        update_event(draft, "event-001", {"endTime": "2026-06-20T08:00:00+09:00"})

    assert draft.events[0].end_time.hour == 10


def test_update_event_reports_missing_event():
    draft = _draft(_event("event-001"))

    with pytest.raises(DraftEditError, match="event-999"):
        update_event(draft, "event-999", {"title": "x"})


def test_delete_event_removes_event_and_question_reference():
    draft = _draft(
        _event("event-001"),
        _event("event-002", title="산책"),
        questions=[_question("event-001", "event-002")],
    )

    removed = delete_event(draft, "event-001")

    assert removed.title == "체류"
    assert [event.client_event_id for event in draft.events] == ["event-002"]
    # 사라진 event 참조만 빠지고 질문 자체는 남는다.
    assert draft.questions[0].related_event_ids == ["event-002"]


def test_delete_event_does_not_renumber_remaining_events():
    """한 계획 안의 다음 도구 호출이 가리키는 id 가 어긋나면 안 된다."""

    draft = _draft(_event("event-001"), _event("event-002"), _event("event-003"))

    delete_event(draft, "event-001")

    assert [event.client_event_id for event in draft.events] == ["event-002", "event-003"]
    assert find_event(draft, "event-003").title == "체류"
