"""식사(MEAL) event 지속시간 가드 검증.

핵심 케이스: 3시간 체류 + 음식 사진 한 장을 통째로 `MEAL` 로 만든 draft 가 들어와도
식사 event 는 1시간을 넘지 않아야 한다.
"""

from datetime import datetime, timedelta

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
from app.services.meal_guard import (
    MEAL_MAX_DURATION,
    MEAL_MIN_DURATION,
    enforce_meal_duration,
)
from tests.fixtures.requests import make_request, notification_item, photo_item, stay_item

# 12:00~15:00 카페 체류 안에 13:20 음식 사진 한 장.
STAY_START = "2026-06-20T12:00:00"
STAY_END = "2026-06-20T15:00:00"
PHOTO_TAKEN = "2026-06-20T13:20:00"

STAY_REF = (EventSourceType.STAY, "stay-1")
PHOTO_REF = (EventSourceType.PHOTO, "photo-1")
NOTIFICATION_REF = (EventSourceType.NOTIFICATION, "noti-1")


def _request(photo_taken=PHOTO_TAKEN, notification_posted=None):
    photos = [photo_item(2, taken=photo_taken, raw_id="photo-1")] if photo_taken else []
    notifications = (
        [notification_item(3, "토스", "결제", posted=notification_posted, raw_id="noti-1")]
        if notification_posted
        else []
    )
    return make_request(
        stays=[stay_item(1, raw_id="stay-1", start=STAY_START, end=STAY_END)],
        photos=photos,
        notifications=notifications,
    )


def _event(*refs, start=STAY_START, end=STAY_END, event_type=EventType.MEAL, confidence=0.8):
    return TimelineEventDraft(
        client_event_id="event-001",
        event_type=event_type,
        title="카페에서 점심 식사",
        start_time=f"{start}+09:00",
        end_time=f"{end}+09:00",
        confidence=confidence,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=st, source_id=sid) for st, sid in refs],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events)
    )


def _duration(event) -> timedelta:
    return event.end_time - event.start_time


def _at(text: str) -> datetime:
    return datetime.fromisoformat(f"{text}+09:00")


# --- 핵심 케이스 --------------------------------------------------------------


def test_three_hour_stay_with_food_photo_never_becomes_an_hour_long_meal():
    # LLM 이 3시간 체류 전체를 하나의 MEAL event 로 만들어 돌려줬다.
    draft = _draft(_event(STAY_REF, PHOTO_REF))
    assert _duration(draft.events[0]) == timedelta(hours=3)

    enforce_meal_duration(draft, _request())

    meal = draft.events[0]
    assert _duration(meal) < timedelta(hours=1)
    assert _duration(meal) >= MEAL_MIN_DURATION
    # 식사 시간은 음식 사진 촬영 시각에 붙는다. 체류 시작(12:00)이 아니다.
    assert meal.start_time == _at(PHOTO_TAKEN)
    assert meal.uncertainty  # 왜 줄였는지 남긴다.


def test_meal_window_is_capped_at_sixty_minutes_between_far_apart_photos():
    request = make_request(
        stays=[stay_item(1, raw_id="stay-1", start=STAY_START, end=STAY_END)],
        photos=[
            photo_item(2, taken="2026-06-20T12:10:00", raw_id="photo-1"),
            photo_item(3, taken="2026-06-20T14:50:00", raw_id="photo-2"),
        ],
    )
    draft = _draft(_event(STAY_REF, PHOTO_REF, (EventSourceType.PHOTO, "photo-2")))

    enforce_meal_duration(draft, request)

    meal = draft.events[0]
    assert _duration(meal) == MEAL_MAX_DURATION
    assert meal.start_time == _at("2026-06-20T12:10:00")


def test_payment_notification_also_anchors_the_meal():
    request = _request(photo_taken=None, notification_posted="2026-06-20T13:45:00")
    draft = _draft(_event(STAY_REF, NOTIFICATION_REF))

    enforce_meal_duration(draft, request)

    # 결제 알림 시각이 식사 시점을 가리킨다.
    assert draft.events[0].start_time == _at("2026-06-20T13:45:00")
    assert _duration(draft.events[0]) == MEAL_MIN_DURATION


# --- 시점 근거가 없을 때 ------------------------------------------------------


def test_long_meal_without_any_time_anchor_is_clamped_and_loses_confidence():
    draft = _draft(_event(STAY_REF, confidence=0.9))

    enforce_meal_duration(draft, _request(photo_taken=None))

    meal = draft.events[0]
    assert _duration(meal) == MEAL_MAX_DURATION
    assert meal.start_time == _at(STAY_START)  # 근거가 없으니 체류 시작 기준
    assert meal.confidence <= 0.6  # 식사 시각을 특정할 수 없다는 뜻이다.


def test_photo_outside_the_event_window_is_not_used_as_an_anchor():
    # 사진 촬영 시각이 event 밖이면 식사 시점 근거로 쓰지 않는다.
    draft = _draft(_event(STAY_REF, PHOTO_REF))

    enforce_meal_duration(draft, _request(photo_taken="2026-06-20T18:00:00"))

    assert draft.events[0].start_time == _at(STAY_START)
    assert draft.events[0].confidence <= 0.6


# --- 경계와 비대상 ------------------------------------------------------------


def test_short_meal_is_expanded_to_the_minimum():
    # 사진 한 장이 만든 순간 event.
    draft = _draft(_event(PHOTO_REF, start=PHOTO_TAKEN, end=PHOTO_TAKEN))

    enforce_meal_duration(draft, _request())

    assert _duration(draft.events[0]) == MEAL_MIN_DURATION


def test_meal_inside_the_allowed_range_is_untouched():
    draft = _draft(_event(STAY_REF, PHOTO_REF, start="2026-06-20T13:00:00", end="2026-06-20T13:40:00"))

    enforce_meal_duration(draft, _request())

    assert _duration(draft.events[0]) == timedelta(minutes=40)
    assert draft.events[0].uncertainty == []
    assert draft.warnings == []


def test_exactly_sixty_minutes_is_allowed():
    draft = _draft(_event(STAY_REF, start="2026-06-20T13:00:00", end="2026-06-20T14:00:00"))

    enforce_meal_duration(draft, _request())

    assert _duration(draft.events[0]) == MEAL_MAX_DURATION
    assert draft.warnings == []


def test_non_meal_events_are_never_touched():
    draft = _draft(_event(STAY_REF, PHOTO_REF, event_type=EventType.WORK, confidence=0.9))

    enforce_meal_duration(draft, _request())

    # 3시간 업무 체류는 정상이다.
    assert _duration(draft.events[0]) == timedelta(hours=3)
    assert draft.events[0].confidence == pytest.approx(0.9)
    assert draft.warnings == []


# --- 경고 -------------------------------------------------------------------


def test_adjustment_leaves_a_warning_with_before_and_after_minutes():
    draft = _draft(_event(STAY_REF, PHOTO_REF))

    enforce_meal_duration(draft, _request())

    assert len(draft.warnings) == 1
    warning = draft.warnings[0]
    assert warning.severity is TimelineWarningSeverity.MEDIUM
    assert "180분에서 20분으로" in warning.message
    assert "카페에서 점심 식사" in warning.message
    assert warning.source_refs  # 어떤 근거의 event였는지 남긴다.
