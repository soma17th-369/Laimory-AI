"""최종 event 개수 상한 검사 (#118).

프롬프트는 하루를 최대 24개 event 로 구성하라고 지시한다. 지켰는지 재는 코드가 없으면
잘게 쪼개진 하루가 그대로 저장돼도 모른다.

**재기만 한다.** 무엇을 합칠지는 의미 판단이라 코드가 고르지 않는다. 그래서 이 테스트는
event 가 잘리지 않는 것도 함께 확인한다.
"""

from app.schemas import TimelineDraft, TimelineWarningSeverity
from app.services.draft_repair import repair_draft
from app.services.event_count_guard import MAX_EVENT_COUNT, verify_event_count
from tests.fixtures.requests import fixture_raw_id, make_request, photo_item

DAY = "2026-06-20"


def _photo_events(count: int) -> list[dict]:
    """서로 다른 사진 한 장씩을 근거로 하는 순간 event N개. 겹치지도 합쳐지지도 않는다."""

    events = []
    for index in range(count):
        hour, minute = divmod(8 * 60 + index * 10, 60)
        moment = f"{DAY}T{hour:02d}:{minute:02d}:00+09:00"
        events.append(
            {
                "clientEventId": f"event-{index + 1:03d}",
                "eventType": "PHOTO_MOMENT",
                "title": f"사진 {index + 1}",
                "description": "사진을 남겼어요.",
                "startTime": moment,
                "endTime": moment,
                "confidence": 0.8,
                "inferenceLevel": "DIRECT",
                "sourceRefs": [
                    {"sourceType": "PHOTO", "rawId": fixture_raw_id(f"photo-{index}")}
                ],
            }
        )
    return events


def _draft(count: int) -> TimelineDraft:
    return TimelineDraft.model_validate(
        {
            "userId": "user-1234",
            "date": DAY,
            "timezone": "Asia/Seoul",
            "events": _photo_events(count),
            "warnings": [],
        }
    )


def _count_warnings(draft: TimelineDraft) -> list:
    return [w for w in draft.warnings if w.warning_id.startswith("warning-event-count-")]


def test_exactly_the_limit_is_not_warned():
    draft = _draft(MAX_EVENT_COUNT)

    verify_event_count(draft)

    assert _count_warnings(draft) == []


def test_one_over_the_limit_is_warned_and_nothing_is_cut():
    draft = _draft(MAX_EVENT_COUNT + 1)

    verify_event_count(draft)

    [warning] = _count_warnings(draft)
    assert warning.severity is TimelineWarningSeverity.MEDIUM
    assert f"{MAX_EVENT_COUNT + 1}개" in warning.message
    assert f"최대 {MAX_EVENT_COUNT}개" in warning.message
    assert len(draft.events) == MAX_EVENT_COUNT + 1  # 자르지 않는다


def test_repeated_runs_do_not_accumulate():
    draft = _draft(MAX_EVENT_COUNT + 3)

    verify_event_count(draft)
    verify_event_count(draft)

    assert len(_count_warnings(draft)) == 1


def test_warning_disappears_after_repair_merges_events_down():
    draft = _draft(MAX_EVENT_COUNT + 1)
    verify_event_count(draft)
    assert _count_warnings(draft)

    # Repair 가 event 를 합쳐 상한 안으로 들어온 상황.
    draft.events = draft.events[:MAX_EVENT_COUNT]
    verify_event_count(draft)

    assert _count_warnings(draft) == []


def test_confirm_pass_runs_the_count_guard():
    """확정 pass 가 이 guard 를 부른다. 병합·삭제가 끝난 뒤의 개수를 잰다."""

    count = MAX_EVENT_COUNT + 1
    request = make_request(
        photos=[
            photo_item(index, taken=f"{DAY}T{8 + index // 6:02d}:{(index % 6) * 10:02d}:00", raw_id=f"photo-{index}")
            for index in range(count)
        ]
    )
    draft = _draft(count)

    repair_draft(draft, request)

    assert len(draft.events) == count
    assert _count_warnings(draft)
