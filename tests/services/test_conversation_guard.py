"""대화로 만든 event 개수 제한 (#119).

대화 알림으로 만든 event 는 하루 최대 3개다. 넘으면 **알림이 많은 3개만 남기고 나머지는
코드가 지운다.**

처음에는 세기만 하고 Repair 가 고르게 했다. 기준을 알림 수 하나로 정한 뒤에는 LLM 이
판단할 것이 남지 않았다. 실제 LLM 은 알림 수 순서로 지우라는 지시를 4번 중 3번 따르지
않았다.
"""

import pytest

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarningSeverity,
)
from app.services.confirm_report import ConfirmReport
from app.services.conversation_guard import (
    MAX_CONVERSATION_EVENTS,
    conversation_events,
    enforce_conversation_limit,
    notification_count,
)
from app.services.draft_repair import repair_draft
from app.services.timeline_validator import validate_timeline_for_storage
from app.services.validator import renumber_events
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    notification_item,
    stay_item,
)

DAY = "2026-06-20"


def _request():
    notifications = [
        notification_item(f"kakao-{index}", "카카오톡", f"친구{index}", posted=f"{DAY}T10:{index:02d}:00")
        for index in range(1, 13)
    ]
    notifications += [
        notification_item("slack-1", "Slack", "배포 채널", posted=f"{DAY}T11:00:00"),
        notification_item("telegram-1", "Telegram", "민수", posted=f"{DAY}T11:10:00"),
        notification_item("card-1", "현대카드", "결제", posted=f"{DAY}T11:20:00"),
    ]
    return make_request(
        notifications=notifications,
        stays=[stay_item(1, raw_id="stay-1", place="카페")],
    )


def _ref(label: str, source_type=EventSourceType.NOTIFICATION) -> SourceRef:
    return SourceRef(source_type=source_type, raw_id=fixture_raw_id(label))


def _event(
    title: str,
    *refs: SourceRef,
    event_type=EventType.SOCIAL,
    start: str = "10:00",
):
    return TimelineEventDraft(
        client_event_id="event-000",
        event_type=event_type,
        title=title,
        description="연락이 이어졌어요.",
        start_time=f"{DAY}T{start}:00+09:00",
        end_time=f"{DAY}T{start}:30+09:00",
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=list(refs),
    )


def _kakao(title: str, *indexes: int, start: str = "10:00") -> TimelineEventDraft:
    """카카오톡 알림 몇 건을 근거로 한 대화 event."""

    return _event(
        title, *(_ref(f"notification-kakao-{index}") for index in indexes), start=start
    )


def _draft(*events) -> TimelineDraft:
    draft = TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )
    # Timeline Agent 의 draft 처럼 번호가 서로 다르게 한다. 확정 pass 는 중간에 번호를
    # 다시 매기지 않아(#144) 같은 번호의 event 를 보정 기록에서 가려내지 못한다.
    renumber_events(draft)
    return draft


def _titles(draft: TimelineDraft) -> list[str]:
    return [event.title for event in draft.events]


def _limit_warnings(draft: TimelineDraft) -> list:
    return [
        warning
        for warning in draft.warnings
        if warning.warning_id.startswith("warning-conversation-limit-")
    ]


# --- 무엇을 대화로 세는가 ---------------------------------------------------------


def test_messenger_only_event_is_a_conversation():
    draft = _draft(_kakao("친구", 1))

    assert _titles_of(conversation_events(draft, _request())) == ["친구"]


def _titles_of(events) -> list[str]:
    return [event.title for event in events]


def test_work_messenger_counts_as_well():
    draft = _draft(_event("배포", _ref("notification-slack-1"), event_type=EventType.WORK))

    assert len(conversation_events(draft, _request())) == 1


def test_unlisted_app_is_not_counted_as_a_conversation():
    """사전에 없는 앱은 메신저일 수도 아닐 수도 있다. 코드는 정하지 않는다."""

    draft = _draft(_event("민수", _ref("notification-telegram-1")))

    assert conversation_events(draft, _request()) == []


def test_event_mixing_messenger_and_unlisted_app_is_not_counted():
    draft = _draft(
        _event("섞임", _ref("notification-kakao-1"), _ref("notification-telegram-1"))
    )

    assert conversation_events(draft, _request()) == []


def test_event_with_a_payment_app_notification_is_not_a_conversation():
    draft = _draft(
        _event("결제", _ref("notification-kakao-1"), _ref("notification-card-1"))
    )

    assert conversation_events(draft, _request()) == []


def test_event_with_location_evidence_is_a_real_event_not_a_conversation():
    """위치·사진·캘린더 근거가 함께 있으면 대화가 아니라 실제 사건이다."""

    draft = _draft(
        _event(
            "카페에서 만남",
            _ref("notification-kakao-1"),
            _ref("stay-1", EventSourceType.STAY),
        )
    )

    assert conversation_events(draft, _request()) == []


@pytest.mark.parametrize(
    "event_type", [EventType.MEAL, EventType.MOVEMENT, EventType.UNKNOWN]
)
def test_payment_or_reservation_notice_from_a_messenger_is_not_counted(event_type):
    """메신저로 온 결제·예약 안내로 만든 event 는 대화 타입이 아니다."""

    draft = _draft(_event("안내", _ref("notification-kakao-1"), event_type=event_type))

    assert conversation_events(draft, _request()) == []


def test_the_same_notification_cited_twice_counts_once():
    event = _kakao("친구", 1, 1, 2)

    assert notification_count(event) == 2


# --- 3개로 맞춘다 ----------------------------------------------------------------


def test_three_conversations_are_left_alone():
    draft = _draft(_kakao("가", 1), _kakao("나", 2), _kakao("다", 3))

    removed = enforce_conversation_limit(draft, _request())

    assert removed == []
    assert _titles(draft) == ["가", "나", "다"]
    assert _limit_warnings(draft) == []


def test_conversations_with_the_most_notifications_are_kept():
    draft = _draft(
        _kakao("한 줄 안부", 1),
        _kakao("단톡방", 2, 3, 4, 5, 6),
        _kakao("약속", 7, 8),
        _kakao("팀 조율", 9, 10, 11),
        _kakao("짧은 연락", 12),
    )

    removed = enforce_conversation_limit(draft, _request())

    # 남은 event 는 원래 순서 그대로다.
    assert _titles(draft) == ["단톡방", "약속", "팀 조율"]
    assert len(conversation_events(draft, _request())) == MAX_CONVERSATION_EVENTS
    assert sorted(_titles_of(removed)) == ["짧은 연락", "한 줄 안부"]


def test_equal_counts_keep_the_earlier_conversation():
    draft = _draft(
        _kakao("늦은 대화", 1, start="15:00"),
        _kakao("가", 2, 3, start="09:00"),
        _kakao("나", 4, 5, start="10:00"),
        _kakao("이른 대화", 6, start="08:00"),
        _kakao("다", 7, 8, start="11:00"),
    )

    enforce_conversation_limit(draft, _request())

    assert _titles(draft) == ["가", "나", "다"]

    draft = _draft(
        _kakao("늦은 대화", 1, start="15:00"),
        _kakao("가", 2, 3, start="09:00"),
        _kakao("나", 4, 5, start="10:00"),
        _kakao("이른 대화", 6, start="08:00"),
    )

    enforce_conversation_limit(draft, _request())

    assert _titles(draft) == ["가", "나", "이른 대화"]


def test_events_that_are_not_conversations_are_never_removed():
    """사전에 없는 앱의 event 와 실제 사건은 세지도 지우지도 않는다."""

    telegram = _event("민수", _ref("notification-telegram-1"))
    cafe = _event(
        "카페에서 만남",
        _ref("notification-kakao-12"),
        _ref("stay-1", EventSourceType.STAY),
    )
    draft = _draft(
        telegram,
        cafe,
        _kakao("가", 1, 2),
        _kakao("나", 3, 4),
        _kakao("다", 5, 6),
        _kakao("라", 7),
    )

    removed = enforce_conversation_limit(draft, _request())

    assert _titles_of(removed) == ["라"]
    assert _titles(draft) == ["민수", "카페에서 만남", "가", "나", "다"]


def test_removal_is_reported_as_a_low_warning():
    draft = _draft(
        _kakao("가", 1, 2), _kakao("나", 3, 4), _kakao("다", 5, 6), _kakao("라", 7)
    )

    enforce_conversation_limit(draft, _request())

    (warning,) = _limit_warnings(draft)
    # 코드가 고쳤다고 알리는 warning 이다. 고칠 것이 남았다는 뜻이 아니다.
    assert warning.severity is TimelineWarningSeverity.LOW
    assert "4건 중" in warning.message
    assert "1건을 제외했습니다" in warning.message
    assert "라" in warning.message


def test_applying_twice_changes_nothing_more():
    draft = _draft(
        _kakao("가", 1, 2), _kakao("나", 3, 4), _kakao("다", 5, 6), _kakao("라", 7)
    )

    enforce_conversation_limit(draft, _request())
    second = enforce_conversation_limit(draft, _request())

    assert second == []
    assert _titles(draft) == ["가", "나", "다"]
    assert len(_limit_warnings(draft)) == 1


def test_request_without_notifications_is_a_no_op():
    draft = _draft(_event("체류", _ref("stay-1", EventSourceType.STAY)))

    assert enforce_conversation_limit(draft, make_request()) == []
    assert draft.warnings == []


# --- 확정 pass ------------------------------------------------------------------


def _five_conversations() -> TimelineDraft:
    return _draft(
        _kakao("가", 1, 2, 3, start="09:00"),
        _kakao("나", 4, 5, start="10:00"),
        _kakao("다", 6, 7, start="11:00"),
        _kakao("라", 8, start="12:00"),
        _kakao("마", 9, start="13:00"),
    )


def test_confirm_pass_limits_conversations_and_records_what_it_removed():
    """지운 event 는 전체 내용을 기록에 남긴다. 저장되는 결과에는 없다."""

    draft = _five_conversations()
    report = ConfirmReport()

    repair_draft(draft, _request(), report=report)

    assert _titles(draft) == ["가", "나", "다"]
    removed = [item for item in report.removed if item["step"] == "대화 개수 제한"]
    assert sorted(item["event"]["title"] for item in removed) == ["라", "마"]
    assert not any(item["kind"] == "CONVERSATION_EVENTS" for item in report.findings)
    validate_timeline_for_storage(draft, _request())


def test_confirm_pass_does_not_limit_legacy_sets():
    """대화 3개는 Notification v3 가 정한 규칙이다. v1·v2 세트의 결과는 지우지 않는다."""

    draft = _five_conversations()

    repair_draft(draft, _request(), extended=False)

    assert _titles(draft) == ["가", "나", "다", "라", "마"]
    assert _limit_warnings(draft) == []
