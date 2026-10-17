"""최종 event 개수 상한 (#118, #163).

프롬프트는 하루를 최대 10개 event 로 구성하라고 지시한다. 지켰는지 재는 코드가 없으면
잘게 쪼개진 하루가 그대로 저장돼도 모른다.

넘으면 v3 세트는 **근거가 알림뿐인 event 만** 코드가 지운다. 사진·위치·캘린더 근거가 있는
event 는 무엇을 합칠지가 의미 판단이라 코드가 고르지 않는다. 그래서 이 테스트는 그런 event 가
잘리지 않는 것도 함께 확인한다.
"""

from app.schemas import TimelineDraft, TimelineEventDraft, TimelineWarningSeverity
from app.services.confirm_report import ConfirmReport
from app.services.draft_repair import repair_draft
from app.services.event_count_guard import (
    LEGACY_MAX_EVENT_COUNT,
    MAX_EVENT_COUNT,
    enforce_event_count,
    verify_event_count,
)
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    notification_item,
    photo_item,
)

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


def test_limit_is_ten():
    """Timeline·Repair v3 프롬프트가 지시하는 값이다."""

    assert MAX_EVENT_COUNT == 10


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


# --- 예전 세트 --------------------------------------------------------------------
#
# v1·v2 Timeline 프롬프트에는 개수 지시가 없다. 그 세트는 줄이기 전의 값으로 잰다.


def test_legacy_sets_keep_the_old_limit():
    assert LEGACY_MAX_EVENT_COUNT == 24

    draft = _draft(LEGACY_MAX_EVENT_COUNT)
    verify_event_count(draft, limit=LEGACY_MAX_EVENT_COUNT)
    assert _count_warnings(draft) == []

    draft = _draft(LEGACY_MAX_EVENT_COUNT + 1)
    verify_event_count(draft, limit=LEGACY_MAX_EVENT_COUNT)
    [warning] = _count_warnings(draft)
    assert f"최대 {LEGACY_MAX_EVENT_COUNT}개" in warning.message


def _confirm(count: int, *, extended: bool) -> TimelineDraft:
    request = make_request(
        photos=[
            photo_item(
                index,
                taken=f"{DAY}T{8 + index // 6:02d}:{(index % 6) * 10:02d}:00",
                raw_id=f"photo-{index}",
            )
            for index in range(count)
        ]
    )
    draft = _draft(count)
    repair_draft(draft, request, extended=extended)
    return draft


def test_confirm_pass_does_not_warn_legacy_sets_at_the_new_limit():
    """운영 세트(v2)가 지시받은 적 없는 10개 기준으로 warning 을 받지 않는다."""

    count = MAX_EVENT_COUNT + 1

    assert _count_warnings(_confirm(count, extended=False)) == []
    assert _count_warnings(_confirm(count, extended=True))


def test_confirm_pass_warns_legacy_sets_at_the_old_limit():
    assert _count_warnings(_confirm(LEGACY_MAX_EVENT_COUNT + 1, extended=False))


# --- 알림만 근거인 event 정리 (#163) -------------------------------------------------


def _notification_event(client_event_id: str, clock: str, *labels: str) -> TimelineEventDraft:
    return TimelineEventDraft.model_validate(
        {
            "clientEventId": client_event_id,
            "eventType": "UNKNOWN",
            "title": client_event_id,
            "description": "알림을 받았다.",
            "startTime": f"{DAY}T{clock}:00+09:00",
            "endTime": f"{DAY}T{clock}:00+09:00",
            "confidence": 0.8,
            "inferenceLevel": "DIRECT",
            "sourceRefs": [
                {"sourceType": "NOTIFICATION", "rawId": fixture_raw_id(label)} for label in labels
            ],
        }
    )


def test_over_the_limit_notification_only_events_go_first():
    draft = _draft(MAX_EVENT_COUNT)
    draft.events += [
        _notification_event("ICPC 안내", "16:56", "n-1", "n-2", "n-3"),
        _notification_event("이모티콘 반응", "16:38", "n-4"),
        _notification_event("인스타그램 반응", "20:36", "n-5"),
    ]

    removed = enforce_event_count(draft)

    # 알림이 적은 것부터, 같으면 늦게 시작한 것부터 지운다.
    assert [event.title for event in removed] == ["인스타그램 반응", "이모티콘 반응", "ICPC 안내"]
    assert len(draft.events) == MAX_EVENT_COUNT
    [warning] = [w for w in draft.warnings if w.warning_id.startswith("warning-event-trim-")]
    assert warning.severity is TimelineWarningSeverity.LOW


def test_only_as_many_as_the_excess_are_dropped():
    draft = _draft(MAX_EVENT_COUNT - 1)
    draft.events += [
        _notification_event("ICPC 안내", "16:56", "n-1", "n-2"),
        _notification_event("이모티콘 반응", "16:38", "n-4"),
    ]

    removed = enforce_event_count(draft)

    assert [event.title for event in removed] == ["이모티콘 반응"]
    assert "ICPC 안내" in [event.title for event in draft.events]


def test_events_with_other_evidence_are_never_dropped():
    draft = _draft(MAX_EVENT_COUNT + 2)

    assert enforce_event_count(draft) == []
    assert len(draft.events) == MAX_EVENT_COUNT + 2


def _confirm_with_notifications(*, extended: bool, report: ConfirmReport | None = None):
    labels = [f"n-{index}" for index in range(3)]
    request = make_request(
        photos=[
            photo_item(index, taken=f"{DAY}T{8 + index // 6:02d}:{(index % 6) * 10:02d}:00", raw_id=f"photo-{index}")
            for index in range(MAX_EVENT_COUNT)
        ],
        # 사전에 없는 앱이라 대화 개수 제한에는 걸리지 않는다.
        notifications=[
            notification_item(index, "Gmail", "안내", posted=f"{DAY}T{20 + index:02d}:00:00", raw_id=label)
            for index, label in enumerate(labels)
        ],
    )
    draft = _draft(MAX_EVENT_COUNT)
    draft.events += [
        _notification_event(f"안내 {index}", f"{20 + index:02d}:00", label)
        for index, label in enumerate(labels)
    ]
    repair_draft(draft, request, extended=extended, report=report)
    return draft


def test_confirm_pass_trims_notification_only_events_in_v3_and_records_them():
    report = ConfirmReport()

    draft = _confirm_with_notifications(extended=True, report=report)

    assert len(draft.events) == MAX_EVENT_COUNT
    assert _count_warnings(draft) == []
    assert [entry["step"] for entry in report.removed] == ["event 개수 정리"] * 3


def test_confirm_pass_does_not_trim_legacy_sets():
    draft = _confirm_with_notifications(extended=False)

    assert len(draft.events) == MAX_EVENT_COUNT + 3
