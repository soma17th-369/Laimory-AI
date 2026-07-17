"""캘린더-위치 상관 보강 검증.

캘린더 `locationText` 와 STAY 의 `place`/`places`/`address` 가 같은 장소를 가리키는
event 만 confidence 를 올린다. 한쪽 근거만 있거나 장소가 다르면 손대지 않는다.
"""

import pytest

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.services.calendar_location import places_match, reinforce_calendar_location
from tests.fixtures.requests import calendar_item, make_request, stay_item

START = "2026-06-20T09:00:00+09:00"
END = "2026-06-20T10:00:00+09:00"

# 실제 입력(data/input/2026-07-08/2026-07-08.json)에서 가져온 조합.
HOME_LOCATION_TEXT = "집(경기도 오산시 운암로 90)"
HOME_ADDRESS = "경기도 오산시 운암로 90"
HOME_PLACE = "오산운암3단지 주공아파트"


def _request(location_text=HOME_LOCATION_TEXT, place=HOME_PLACE, address=HOME_ADDRESS):
    return make_request(
        calendars=[calendar_item(1, "회의", raw_id="cal-1", location_text=location_text)],
        stays=[stay_item(2, raw_id="stay-1", place=place, address=address, places=[])],
    )


def _event(*refs, confidence=0.7) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id="event-001",
        event_type=EventType.CALENDAR_EVENT,
        title="회의",
        start_time=START,
        end_time=END,
        confidence=confidence,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=st, source_id=sid) for st, sid in refs],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events)
    )


CALENDAR_REF = (EventSourceType.CALENDAR, "cal-1")
STAY_REF = (EventSourceType.STAY, "stay-1")


# --- places_match ------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # 캘린더 메모가 주소를 통째로 품는다(실제 입력 조합).
        (HOME_LOCATION_TEXT, HOME_ADDRESS),
        # 체류 장소명이 캘린더 메모를 품는다.
        ("용산 서울드래곤시티", "서울드래곤시티 그랜드볼룸"),
        # 의미 있는 토큰을 공유한다.
        ("강남역 스타벅스", "스타벅스 강남점"),
        # 구분 기호와 대소문자는 무시한다.
        ("Starbucks-Gangnam", "starbucks gangnam"),
    ],
)
def test_places_match_true(left, right):
    assert places_match(left, right)
    assert places_match(right, left)  # 대칭


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (HOME_LOCATION_TEXT, "서울특별시 강남구 테헤란로 90"),
        ("회의실", "경기도 오산시 운암로 90"),
        (None, HOME_ADDRESS),
        (HOME_LOCATION_TEXT, None),
        ("", HOME_ADDRESS),
    ],
)
def test_places_match_false(left, right):
    assert not places_match(left, right)


def test_digit_only_tokens_do_not_match():
    # 번지수만 같은 서로 다른 주소가 같은 장소로 오인되면 안 된다.
    assert not places_match("테헤란로 152", "운암로 152")


# --- reinforce_calendar_location ---------------------------------------------


def test_confidence_is_boosted_when_calendar_and_stay_point_to_the_same_place():
    draft = _draft(_event(CALENDAR_REF, STAY_REF, confidence=0.7))

    reinforce_calendar_location(draft, _request())

    assert draft.events[0].confidence == pytest.approx(0.8)


def test_stay_place_name_alone_is_enough_to_match():
    draft = _draft(_event(CALENDAR_REF, STAY_REF, confidence=0.6))
    request = _request(location_text="오산운암3단지 주공아파트 앞", address=None)

    reinforce_calendar_location(draft, request)

    assert draft.events[0].confidence == pytest.approx(0.7)


def test_no_boost_when_places_differ():
    draft = _draft(_event(CALENDAR_REF, STAY_REF, confidence=0.7))
    request = _request(location_text="서울 강남구 테헤란로 152", place="판교역", address="경기도 성남시")

    reinforce_calendar_location(draft, request)

    assert draft.events[0].confidence == pytest.approx(0.7)


def test_no_boost_without_a_stay_ref():
    draft = _draft(_event(CALENDAR_REF, confidence=0.7))

    reinforce_calendar_location(draft, _request())

    assert draft.events[0].confidence == pytest.approx(0.7)


def test_no_boost_without_a_calendar_ref():
    draft = _draft(_event(STAY_REF, confidence=0.7))

    reinforce_calendar_location(draft, _request())

    assert draft.events[0].confidence == pytest.approx(0.7)


def test_no_boost_when_calendar_has_no_location_text():
    draft = _draft(_event(CALENDAR_REF, STAY_REF, confidence=0.7))

    reinforce_calendar_location(draft, _request(location_text=None))

    assert draft.events[0].confidence == pytest.approx(0.7)


def test_boost_is_capped():
    draft = _draft(_event(CALENDAR_REF, STAY_REF, confidence=0.92))

    reinforce_calendar_location(draft, _request())

    # 교차 검증이 됐어도 1.0 으로 단정하지 않는다.
    assert draft.events[0].confidence == pytest.approx(0.95)


def test_unreferenced_events_are_untouched():
    unrelated = _event((EventSourceType.PHOTO, "photo-1"), confidence=0.5)
    matched = _event(CALENDAR_REF, STAY_REF, confidence=0.5)
    draft = _draft(unrelated, matched)

    reinforce_calendar_location(draft, _request())

    assert draft.events[0].confidence == pytest.approx(0.5)
    assert draft.events[1].confidence == pytest.approx(0.6)
