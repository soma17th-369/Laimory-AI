"""대화로 만든 event 개수 검사 (#119).

대화 event 는 하루 최대 3개다. 세는 기준은 Notification Agent 가 쓰는 구분과 같다 —
메신저 정책이 있는 앱의 알림은 대화로 세고, **사전에 없는 앱의 알림은 대화로 세지 않는다.**
그 앱이 무엇인지 코드는 모른다.

**세기만 한다.** 무엇이 중요한 대화인지는 내용을 봐야 알므로 지우지 않는다.
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
from app.services.conversation_guard import (
    MAX_CONVERSATION_EVENTS,
    count_conversation_events,
    verify_conversation_limit,
)
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    notification_item,
    stay_item,
)

DAY = "2026-06-20"


def _request():
    notifications = [
        notification_item(f"kakao-{index}", "카카오톡", f"친구{index}")
        for index in range(1, 6)
    ]
    notifications += [
        notification_item("slack-1", "Slack", "배포 채널"),
        notification_item("telegram-1", "Telegram", "민수"),
        notification_item("card-1", "현대카드", "결제"),
    ]
    return make_request(
        notifications=notifications,
        stays=[stay_item(1, raw_id="stay-1", place="카페")],
    )


def _ref(label: str, source_type=EventSourceType.NOTIFICATION) -> SourceRef:
    return SourceRef(source_type=source_type, raw_id=fixture_raw_id(label))


def _event(client_event_id: str, *refs: SourceRef, event_type=EventType.SOCIAL):
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=f"대화 {client_event_id}",
        description="연락이 이어졌어요.",
        start_time=f"{DAY}T10:00:00+09:00",
        end_time=f"{DAY}T10:30:00+09:00",
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=list(refs),
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )


def _kakao_events(count: int) -> list[TimelineEventDraft]:
    return [
        _event(f"event-{index:03d}", _ref(f"notification-kakao-{index}"))
        for index in range(1, count + 1)
    ]


def _limit_warnings(draft: TimelineDraft) -> list:
    return [
        warning
        for warning in draft.warnings
        if warning.warning_id.startswith("warning-conversation-limit-")
    ]


# --- 무엇을 대화로 세는가 ---------------------------------------------------------


def test_messenger_only_event_is_a_conversation():
    draft = _draft(_event("event-001", _ref("notification-kakao-1")))

    count = count_conversation_events(draft, _request())

    assert [event.client_event_id for event in count.conversations] == ["event-001"]
    assert count.undetermined == []


def test_work_messenger_counts_as_well():
    draft = _draft(
        _event("event-001", _ref("notification-slack-1"), event_type=EventType.WORK)
    )

    assert len(count_conversation_events(draft, _request()).conversations) == 1


def test_unlisted_app_is_not_counted_as_a_conversation():
    """사전에 없는 앱은 메신저일 수도 아닐 수도 있다. 코드는 정하지 않는다."""

    draft = _draft(_event("event-001", _ref("notification-telegram-1")))

    count = count_conversation_events(draft, _request())

    assert count.conversations == []
    assert [event.client_event_id for event in count.undetermined] == ["event-001"]


def test_event_mixing_messenger_and_unlisted_app_is_undetermined():
    draft = _draft(
        _event(
            "event-001",
            _ref("notification-kakao-1"),
            _ref("notification-telegram-1"),
        )
    )

    count = count_conversation_events(draft, _request())

    assert count.conversations == []
    assert len(count.undetermined) == 1


def test_event_with_a_payment_app_notification_is_not_a_conversation():
    draft = _draft(
        _event("event-001", _ref("notification-kakao-1"), _ref("notification-card-1"))
    )

    count = count_conversation_events(draft, _request())

    assert count.conversations == []
    assert count.undetermined == []


def test_event_with_location_evidence_is_a_real_event_not_a_conversation():
    """위치·사진·캘린더 근거가 함께 있으면 대화가 아니라 실제 사건이다."""

    draft = _draft(
        _event(
            "event-001",
            _ref("notification-kakao-1"),
            _ref("stay-1", EventSourceType.STAY),
        )
    )

    count = count_conversation_events(draft, _request())

    assert count.conversations == []
    assert count.undetermined == []


@pytest.mark.parametrize(
    "event_type", [EventType.MEAL, EventType.MOVEMENT, EventType.UNKNOWN]
)
def test_payment_or_reservation_notice_from_a_messenger_is_not_counted(event_type):
    """메신저로 온 결제·예약 안내로 만든 event 는 대화 타입이 아니다."""

    draft = _draft(
        _event("event-001", _ref("notification-kakao-1"), event_type=event_type)
    )

    assert count_conversation_events(draft, _request()).conversations == []


# --- 상한 ----------------------------------------------------------------------


def test_three_conversations_are_within_the_limit():
    draft = _draft(*_kakao_events(MAX_CONVERSATION_EVENTS))

    count = verify_conversation_limit(draft, _request())

    assert not count.over_limit
    assert not count.needs_review
    assert _limit_warnings(draft) == []


def test_four_conversations_are_warned():
    draft = _draft(*_kakao_events(4))

    count = verify_conversation_limit(draft, _request())

    assert count.over_limit
    (warning,) = _limit_warnings(draft)
    assert warning.severity is TimelineWarningSeverity.MEDIUM
    assert "4개" in warning.message


def test_undetermined_events_ask_for_review_without_a_warning():
    """코드가 아는 대화는 3개뿐이다. 넘었다고 단정하지 않고 Repair 에 넘긴다."""

    draft = _draft(
        *_kakao_events(3),
        _event("event-004", _ref("notification-telegram-1")),
    )

    count = verify_conversation_limit(draft, _request())

    assert not count.over_limit
    assert count.needs_review
    assert _limit_warnings(draft) == []


def test_guard_never_removes_events():
    draft = _draft(*_kakao_events(5))

    verify_conversation_limit(draft, _request())

    assert len(draft.events) == 5


def test_detail_lists_events_with_their_notification_counts():
    busy = _event(
        "event-001",
        _ref("notification-kakao-1"),
        _ref("notification-kakao-2"),
    )
    draft = _draft(busy, _event("event-002", _ref("notification-telegram-1")))

    detail = count_conversation_events(draft, _request()).detail()

    assert detail["limit"] == 3
    assert detail["conversationEvents"] == [
        {"clientEventId": "event-001", "title": "대화 event-001", "notificationCount": 2}
    ]
    assert [item["clientEventId"] for item in detail["undeterminedEvents"]] == ["event-002"]


def test_repeated_runs_do_not_accumulate():
    draft = _draft(*_kakao_events(4))

    verify_conversation_limit(draft, _request())
    verify_conversation_limit(draft, _request())

    assert len(_limit_warnings(draft)) == 1


def test_warning_disappears_after_repair_trims_conversations():
    draft = _draft(*_kakao_events(4))
    verify_conversation_limit(draft, _request())
    assert _limit_warnings(draft)

    draft.events = draft.events[:3]
    verify_conversation_limit(draft, _request())

    assert _limit_warnings(draft) == []


def test_request_without_notifications_is_a_no_op():
    draft = _draft(_event("event-001", _ref("stay-1", EventSourceType.STAY)))

    count = verify_conversation_limit(draft, make_request())

    assert count.conversations == []
    assert draft.warnings == []
