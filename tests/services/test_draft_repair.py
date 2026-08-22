"""Timeline draft 결정론적 repair 검증.

정렬·clientEventId 부여·지속시간·겹침을 LLM 이 아니라 코드가 확정하는지 본다.
특히 "긴 체류 안의 짧은 식사" 같은 **포함 관계**를 겹침으로 오인해 뭉개지 않아야 한다.
"""

from datetime import timedelta

import pytest

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineQuestion,
    TimelineWarningSeverity,
)
from app.services.draft_repair import (
    repair_draft,
    repair_durations,
    resolve_overlaps,
    sort_events,
)
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    movement_item,
    notification_item,
    photo_item,
    sleep_item,
    stay_item,
    steps_item,
)

DAY = "2026-06-20"


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _event(
    client_event_id,
    start,
    end,
    *refs,
    event_type=EventType.REST,
    title=None,
    confidence=0.7,
    place=None,
) -> TimelineEventDraft:
    # sourceRefs 는 스키마상 최소 1개다. 근거가 관심사가 아닌 테스트는 STAY 하나로 둔다.
    used = refs or ((EventSourceType.STAY, "stay-1"),)
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title or f"{event_type.value} {start}",
        start_time=_t(start),
        end_time=_t(end),
        confidence=confidence,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        place=place,
        source_refs=[
            SourceRef(source_type=st, raw_id=fixture_raw_id(raw_id))
            for st, raw_id in used
        ],
    )


def _draft(*events, questions=()) -> TimelineDraft:
    return TimelineDraft(
        user_id="u",
        date=DAY,
        timezone="Asia/Seoul",
        events=list(events),
        questions=list(questions),
    )


STAY_REF = (EventSourceType.STAY, "stay-1")
PHOTO_REF = (EventSourceType.PHOTO, "photo-1")
SLEEP_REF = (EventSourceType.SLEEP, "sleep-1")
MOVE_REF = (EventSourceType.MOVEMENT, "move-1")
CALENDAR_REF = (EventSourceType.CALENDAR, "cal-1")


def _request():
    return make_request(
        stays=[stay_item(1, raw_id="stay-1", start=f"{DAY}T12:00:00", end=f"{DAY}T15:00:00")],
        movements=[movement_item(2, raw_id="move-1", start=f"{DAY}T23:31:00", end=f"{DAY}T23:53:00")],
        photos=[photo_item(3, taken=f"{DAY}T13:20:00", raw_id="photo-1")],
        healths=[
            sleep_item(4, f"{DAY}T01:10:00", f"{DAY}T06:50:00", 340, raw_id="sleep-1"),
            # 하루 전체를 덮는 걸음 수 집계.
            steps_item(5, 10000, start=f"{DAY}T00:00:00", end=f"{DAY}T23:59:00", raw_id="steps-1"),
        ],
    )


def _titles(draft) -> list[str]:
    return [event.title for event in draft.events]


def _ids(draft) -> list[str]:
    return [event.client_event_id for event in draft.events]


# --- 정렬 --------------------------------------------------------------------


def test_events_are_sorted_by_start_time():
    draft = _draft(
        _event("event-001", "15:00", "16:00", title="오후"),
        _event("event-002", "09:00", "10:00", title="오전"),
        _event("event-003", "12:00", "13:00", title="점심때"),
    )

    sort_events(draft)

    assert _titles(draft) == ["오전", "점심때", "오후"]


def test_same_start_puts_the_shorter_event_first():
    # 긴 배경 event 가 그 안의 짧은 사건을 감싸는 모양이 되지 않도록, 짧은 쪽을 먼저 둔다.
    draft = _draft(
        _event("event-001", "09:00", "23:00", title="긴 배경"),
        _event("event-002", "09:00", "09:30", title="짧은 사건"),
    )

    sort_events(draft)

    assert _titles(draft) == ["짧은 사건", "긴 배경"]


def test_identical_span_breaks_the_tie_by_confidence():
    draft = _draft(
        _event("event-001", "09:00", "10:00", title="덜 확실", confidence=0.4),
        _event("event-002", "09:00", "10:00", title="더 확실", confidence=0.9),
    )

    sort_events(draft)

    assert _titles(draft) == ["더 확실", "덜 확실"]


def test_sorting_is_deterministic_for_fully_tied_events():
    first = _draft(
        _event("event-001", "09:00", "10:00", title="나중", confidence=0.5),
        _event("event-002", "09:00", "10:00", title="가나다", confidence=0.5),
    )
    second = _draft(
        _event("event-001", "09:00", "10:00", title="가나다", confidence=0.5),
        _event("event-002", "09:00", "10:00", title="나중", confidence=0.5),
    )

    sort_events(first)
    sort_events(second)

    assert _titles(first) == _titles(second)


# --- clientEventId 재부여 ------------------------------------------------------


def test_client_event_ids_are_assigned_after_sorting_and_questions_follow():
    draft = _draft(
        _event("event-001", "15:00", "16:00", STAY_REF, title="오후"),
        _event("event-002", "09:00", "10:00", PHOTO_REF, title="오전"),
        questions=[
            TimelineQuestion(
                question_id="question-001",
                time_range={"startTime": _t("15:00"), "endTime": _t("16:00")},
                question="오후 3시쯤 무엇을 했나요?",
                reason="확인 필요",
                related_event_ids=["event-001"],  # 정렬 전의 '오후' event
            )
        ],
    )

    repair_draft(draft, _request())

    assert _titles(draft) == ["오전", "오후"]
    assert _ids(draft) == ["event-001", "event-002"]
    # 질문은 여전히 '오후' event 를 가리켜야 한다. 새 id 는 event-002 다.
    assert draft.questions[0].related_event_ids == ["event-002"]


# --- 지속시간 repair ----------------------------------------------------------


def test_wake_up_spanning_time_is_collapsed_to_an_instant():
    draft = _draft(_event("event-001", "06:50", "08:00", SLEEP_REF, event_type=EventType.WAKE_UP))

    repair_durations(draft, _request())

    event = draft.events[0]
    assert event.start_time == event.end_time
    assert any("순간이어야 할 event" in w.message for w in draft.warnings)


def test_zero_duration_event_is_restored_from_its_source_span():
    # LLM 이 이동 event 를 0분으로 만들었다. 원본 MOVEMENT 는 23:31~23:53 이다.
    draft = _draft(_event("event-001", "23:31", "23:31", MOVE_REF, event_type=EventType.MOVEMENT))

    repair_durations(draft, _request())

    event = draft.events[0]
    assert event.end_time - event.start_time == timedelta(minutes=22)
    assert any("근거 원본 시간으로 복원" in w.message for w in draft.warnings)


def test_photo_moment_may_stay_an_instant():
    draft = _draft(_event("event-001", "13:20", "13:20", PHOTO_REF, event_type=EventType.PHOTO_MOMENT))

    repair_durations(draft, _request())

    assert draft.events[0].start_time == draft.events[0].end_time
    assert draft.warnings == []


def test_zero_duration_without_a_restorable_source_is_reported_not_invented():
    # 사진 근거뿐이라 구간을 복원할 수 없다. 없는 시간을 지어내지 않는다.
    draft = _draft(_event("event-001", "13:20", "13:20", PHOTO_REF, event_type=EventType.REST))

    repair_durations(draft, _request())

    event = draft.events[0]
    assert event.start_time == event.end_time
    assert event.uncertainty
    assert any(
        w.severity is TimelineWarningSeverity.MEDIUM and "복원할 수 없는" in w.message
        for w in draft.warnings
    )


def test_daily_step_aggregate_is_never_used_to_restore_a_span():
    # ACTIVITY 는 하루 전체 집계라, 이것으로 복원하면 event 가 하루가 되어 버린다.
    draft = _draft(
        _event("event-001", "13:20", "13:20", (EventSourceType.ACTIVITY, "steps-1"), event_type=EventType.EXERCISE)
    )

    repair_durations(draft, _request())

    assert draft.events[0].start_time == draft.events[0].end_time


def test_meal_duration_is_left_to_the_meal_guard():
    draft = _draft(_event("event-001", "12:00", "15:00", STAY_REF, PHOTO_REF, event_type=EventType.MEAL))

    repair_durations(draft, _request())

    # repair_durations 는 MEAL 을 건드리지 않는다(3시간 그대로).
    assert draft.events[0].end_time - draft.events[0].start_time == timedelta(hours=3)


# --- 겹침 정리 ----------------------------------------------------------------


def test_duplicate_events_at_the_same_place_are_merged():
    draft = _draft(
        _event("event-001", "14:26", "14:36", STAY_REF, place="집", title="집에 머문 시간", confidence=0.6),
        _event("event-002", "14:30", "15:00", PHOTO_REF, place="집", title="집에서 쉰 시간", confidence=0.9),
    )

    resolve_overlaps(draft)

    assert len(draft.events) == 1
    merged = draft.events[0]
    assert merged.title == "집에서 쉰 시간"  # 확신이 높은 쪽 서술을 남긴다
    assert merged.confidence == pytest.approx(0.9)
    assert merged.start_time.isoformat() == _t("14:26")
    assert merged.end_time.isoformat() == _t("15:00")
    assert merged.end_time - merged.start_time == timedelta(minutes=34)
    assert len(merged.source_refs) == 2  # 근거는 합집합
    assert any("중복 event" in w.message for w in draft.warnings)


def test_a_short_meal_nested_in_a_long_stay_is_never_merged_away():
    # 우리가 프롬프트로 요구한 정상적인 모양이다. 겹침 정리가 이것을 뭉개면 안 된다.
    draft = _draft(
        _event("event-001", "12:00", "15:00", STAY_REF, place="카페", title="카페에서 보낸 오후"),
        _event("event-002", "13:20", "13:50", PHOTO_REF, event_type=EventType.MEAL, place="카페", title="카페에서 점심"),
    )

    resolve_overlaps(draft)

    assert len(draft.events) == 2
    assert draft.warnings == []  # 포함 관계는 충돌이 아니다


def test_partial_overlap_between_different_places_is_warned_not_trimmed():
    draft = _draft(
        _event("event-001", "14:26", "14:36", STAY_REF, place="집", title="집에 머문 시간"),
        _event("event-002", "14:33", "15:07", PHOTO_REF, event_type=EventType.MEAL, place="배스킨라빈스", title="아이스크림"),
    )

    resolve_overlaps(draft)

    # 어느 쪽이 맞는지 코드가 알 수 없으므로 시간을 자르지 않는다.
    assert len(draft.events) == 2
    assert draft.events[0].end_time.isoformat() == _t("14:36")
    assert draft.events[1].start_time.isoformat() == _t("14:33")
    assert any("서로 겹치는 event" in w.message for w in draft.warnings)


def test_touching_boundaries_are_not_an_overlap():
    draft = _draft(
        _event("event-001", "22:33", "23:31", STAY_REF, place="집"),
        _event("event-002", "23:31", "23:53", MOVE_REF, event_type=EventType.MOVEMENT, place="집"),
    )

    resolve_overlaps(draft)

    assert len(draft.events) == 2
    assert draft.warnings == []


# --- 전체 repair 파이프라인 ----------------------------------------------------


def test_an_unknown_raw_id_drops_the_unsupported_event():
    # 입력에 없는 UUID rawId는 환각으로 보고, 유효한 근거가 남지 않은 event를 제외한다.
    draft = _draft(
        _event(
            "event-001",
            "09:00",
            "10:00",
            (
                EventSourceType.STAY,
                "f0472779-54a0-49c8-8386-f391ba7ac789",
            ),
            title="환각",
        ),
        _event("event-002", "11:00", "12:00", STAY_REF, title="정상"),
    )

    repair_draft(draft, _request())

    assert _titles(draft) == ["정상"]
    assert _ids(draft) == ["event-001"]
    assert any("입력에 없는 rawId 참조" in warning.message for warning in draft.warnings)


def test_repair_fills_place_and_drops_an_unsupported_address():
    request = make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                place="두꺼비 감자탕 지산점",
                address="경기도 오산시 운암로 90",
                places=[],
                start=f"{DAY}T12:00:00",
                end=f"{DAY}T15:00:00",
            )
        ],
    )
    draft = _draft(
        _event("event-001", "12:00", "13:00", STAY_REF, title="한 곳에서 머문 시간"),
    )
    draft.events[0].place = "한 곳"
    draft.events[0].address = "서울특별시 강남구 테헤란로 152"

    repair_draft(draft, request)

    event = draft.events[0]
    assert event.place == "두꺼비 감자탕 지산점"  # 얼버무림 → 근거의 장소명
    assert event.address == "경기도 오산시 운암로 90"  # 지어낸 주소 → 근거의 주소
    assert any("정확한 입력 근거가 없는 주소" in w.message for w in draft.warnings)


def test_window_warning_names_the_event_by_title_not_a_stale_id():
    # window 검증은 정렬·id 재부여보다 앞에서 돈다. 그때 적어 둔 clientEventId 는
    # 사용자가 볼 때쯤이면 다른 event 를 가리키므로 제목으로 알려야 한다.
    # 체류가 window 를 넘어 이어진다. event 시간은 근거 안이므로 align 이 자르지 않고,
    # window 검증이 경계에서 자른다.
    request = make_request(
        window={"start": f"{DAY}T10:00:00", "end": f"{DAY}T12:00:00"},
        stays=[stay_item(1, raw_id="stay-1", start=f"{DAY}T10:00:00", end=f"{DAY}T13:00:00")],
        photos=[photo_item(2, taken=f"{DAY}T10:10:00", raw_id="photo-2")],
    )
    draft = _draft(
        _event("event-001", "11:30", "13:00", STAY_REF, title="경계에 걸친 오후"),
        _event(
            "event-002",
            "10:10",
            "10:20",
            (EventSourceType.PHOTO, "photo-2"),
            title="온전한 오전",
        ),
    )

    repair_draft(draft, request)

    assert _titles(draft) == ["온전한 오전", "경계에 걸친 오후"]
    boundary = next(w for w in draft.warnings if "경계에 걸친 event" in w.message)
    assert "경계에 걸친 오후" in boundary.message
    assert "event-0" not in boundary.message


def test_repair_shrinks_an_over_long_meal_and_keeps_everything_sorted():
    draft = _draft(
        _event("event-001", "12:00", "15:00", STAY_REF, PHOTO_REF, event_type=EventType.MEAL, title="점심"),
        _event("event-002", "09:00", "10:00", STAY_REF, title="오전"),
    )

    repair_draft(draft, _request())

    assert _titles(draft) == ["오전", "점심"]
    assert _ids(draft) == ["event-001", "event-002"]
    meal = draft.events[1]
    assert meal.end_time - meal.start_time < timedelta(hours=1)
    # 식사는 음식 사진 시각(13:20)에 붙는다.
    assert meal.start_time.isoformat() == f"{DAY}T13:20:00+09:00"


# --- 근거 구간 정렬(align) -----------------------------------------------------


def test_a_stay_only_event_cannot_claim_time_the_stay_does_not_cover():
    # 실제 사례: 유일한 근거인 STAY 가 22:33 에 시작하는데 LLM 은 22:07 부터라고 적었다.
    request = make_request(
        stays=[stay_item(1, raw_id="stay-1", start=f"{DAY}T22:33:00", end=f"{DAY}T23:31:00")]
    )
    draft = _draft(_event("event-001", "22:07", "23:31", STAY_REF, title="늦은 밤"))

    repair_draft(draft, request)

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("22:33")
    assert event.end_time.isoformat() == _t("23:31")
    assert any("근거에 맞췄습니다" in w.message for w in draft.warnings)


def _walk_request():
    return make_request(
        stays=[stay_item(1, raw_id="stay-1", start=f"{DAY}T21:54:00", end=f"{DAY}T22:00:00")],
        movements=[
            movement_item(2, raw_id="out", start=f"{DAY}T21:39:00", end=f"{DAY}T21:54:00"),
            movement_item(3, raw_id="back", start=f"{DAY}T22:00:00", end=f"{DAY}T22:07:00"),
        ],
    )


def _walk_event(*refs) -> TimelineEventDraft:
    return _event(
        "event-001",
        "21:39",
        "21:54",
        *refs,
        event_type=EventType.EXERCISE,
        title="저녁 무렵 동네 한 바퀴 산책",
    )


def test_an_event_citing_a_movement_must_cover_the_whole_round_trip():
    # 실제 사례: 산책 event 가 복귀 이동(22:07 종료)을 근거로 대 놓고 21:54 에서 끊겼다.
    draft = _draft(
        _walk_event(
            (EventSourceType.MOVEMENT, "out"),
            STAY_REF,
            (EventSourceType.MOVEMENT, "back"),
        )
    )

    repair_draft(draft, _walk_request())

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("21:39")
    assert event.end_time.isoformat() == _t("22:07")  # 돌아온 시각까지


def test_a_movement_mislabelled_as_a_stay_is_still_found_by_its_raw_id():
    # 실제 사례: LLM 이 왕복 이동 rawId 를 정확히 인용해 놓고 타입을 `STAY` 라고 적었다.
    # rawId 는 UUID 라 유일하므로 입력의 실제 타입을 믿는다. 그러지 않으면 산책이
    # 편도에서 끊긴다.
    draft = _draft(
        _walk_event(
            (EventSourceType.STAY, "out"),  # 실제로는 MOVEMENT
            STAY_REF,
            (EventSourceType.STAY, "back"),  # 실제로는 MOVEMENT
        )
    )

    repair_draft(draft, _walk_request())

    event = draft.events[0]
    assert event.start_time.isoformat() == _t("21:39")
    assert event.end_time.isoformat() == _t("22:07")
    types = {ref.source_type for ref in event.source_refs}
    assert types == {EventSourceType.STAY, EventSourceType.MOVEMENT}


def test_a_short_meal_is_not_stretched_to_the_stay_it_cites_for_its_place():
    # timeline.md §1-5 는 식사 event 가 장소를 말하려고 같은 STAY 를 참조하는 것을 허용한다.
    # 체류를 참조했다는 이유로 식사를 체류 전체로 늘리면 그 구조가 무너진다.
    draft = _draft(
        _event("event-001", "13:20", "13:50", STAY_REF, PHOTO_REF, event_type=EventType.MEAL, title="점심")
    )

    repair_draft(draft, _request())  # STAY 는 12:00~15:00

    meal = draft.events[0]
    assert meal.start_time.isoformat() == _t("13:20")
    assert meal.end_time.isoformat() == _t("13:50")


# --- 끊긴 체류 병합 ------------------------------------------------------------


def test_fragmented_stays_at_the_same_place_become_one_continuous_event():
    # 실제 사례: 집에서 나가지 않았는데 8분·9분짜리 event 두 개가 남고 그 사이 세 시간이
    # 하루에서 사라졌다.
    request = make_request(
        stays=[
            stay_item(1, raw_id="stay-a", start=f"{DAY}T11:12:00", end=f"{DAY}T11:20:00", place="집", places=[]),
            stay_item(2, raw_id="stay-b", start=f"{DAY}T14:26:00", end=f"{DAY}T14:36:00", place="집", places=[]),
        ]
    )
    draft = _draft(
        _event("event-001", "11:12", "11:20", (EventSourceType.STAY, "stay-a"), title="집에 잠깐"),
        _event("event-002", "14:26", "14:36", (EventSourceType.STAY, "stay-b"), title="집에서 보낸 오후", confidence=0.9),
    )

    repair_draft(draft, request)

    assert len(draft.events) == 1
    merged = draft.events[0]
    assert merged.title == "집에서 보낸 오후"  # 확신이 높은 쪽 서술
    assert merged.start_time.isoformat() == _t("11:12")
    assert merged.end_time.isoformat() == _t("14:36")
    assert len(merged.source_refs) == 2  # 두 체류를 모두 근거로 남긴다
    assert any("이동 없이 같은 장소에서 이어진 체류" in w.message for w in draft.warnings)


def test_only_pure_stay_events_are_merged_not_what_happened_inside_them():
    # 실제 사례: 배경 캘린더 event 와 사진 event 가 같은 체류를 참조했다는 이유로
    # 빨려 들어가 `09:00~23:00 WORK 배스킨라빈스 아이스크림을 사서 먹은 오후` 가 나왔다.
    # 체류를 참조했다고 다 체류 조각인 것이 아니다. 그 안에서 일어난 사건일 수 있다.
    request = make_request(
        stays=[
            stay_item(1, raw_id="stay-a", start=f"{DAY}T11:12:00", end=f"{DAY}T11:20:00", place="집", places=[]),
            stay_item(2, raw_id="stay-b", start=f"{DAY}T14:26:00", end=f"{DAY}T14:36:00", place="집", places=[]),
        ],
        calendars=[calendar_item(3, "ASM 프로젝트 MVP 개발", start=f"{DAY}T09:00:00", end=f"{DAY}T23:00:00", raw_id="cal-1")],
        photos=[photo_item(4, taken=f"{DAY}T14:33:00", raw_id="photo-1")],
    )
    stay_a = (EventSourceType.STAY, "stay-a")
    stay_b = (EventSourceType.STAY, "stay-b")
    draft = _draft(
        _event("event-001", "09:00", "23:00", CALENDAR_REF, stay_a, event_type=EventType.WORK, title="ASM 프로젝트 MVP 개발", confidence=0.5),
        _event("event-002", "11:12", "11:20", stay_a, title="집에서 보낸 오전"),
        _event("event-003", "14:26", "14:36", stay_b, title="집에서 보낸 오후"),
        _event("event-004", "14:33", "15:07", PHOTO_REF, stay_b, event_type=EventType.PHOTO_MOMENT, title="배스킨라빈스 아이스크림", confidence=0.95),
    )

    repair_draft(draft, request)

    titles = _titles(draft)
    assert "ASM 프로젝트 MVP 개발" in titles  # 배경 일정은 삼켜지지 않는다
    assert "배스킨라빈스 아이스크림" in titles  # 체류 안의 사건도 남는다
    assert len(draft.events) == 3  # 순수 체류 두 개만 하나로

    background = next(e for e in draft.events if e.title == "ASM 프로젝트 MVP 개발")
    assert background.event_type is EventType.WORK
    assert background.start_time.isoformat() == _t("09:00")

    stay = next(e for e in draft.events if e.title == "집에서 보낸 오전")
    assert stay.start_time.isoformat() == _t("11:12")
    assert stay.end_time.isoformat() == _t("14:36")


def test_stays_separated_by_a_movement_stay_separate():
    request = make_request(
        stays=[
            stay_item(1, raw_id="stay-a", start=f"{DAY}T11:00:00", end=f"{DAY}T12:00:00", place="집", places=[]),
            stay_item(2, raw_id="stay-b", start=f"{DAY}T14:00:00", end=f"{DAY}T15:00:00", place="집", places=[]),
        ],
        movements=[movement_item(3, raw_id="out", start=f"{DAY}T12:30:00", end=f"{DAY}T13:00:00")],
    )
    draft = _draft(
        _event("event-001", "11:00", "12:00", (EventSourceType.STAY, "stay-a"), title="나가기 전"),
        _event("event-002", "14:00", "15:00", (EventSourceType.STAY, "stay-b"), title="돌아온 뒤"),
    )

    repair_draft(draft, request)

    assert _titles(draft) == ["나가기 전", "돌아온 뒤"]


# --- 수면 경계 ---------------------------------------------------------------


def test_repair_pushes_an_event_started_by_a_sleeping_notification_to_the_wake_time():
    request = make_request(
        healths=[sleep_item(1, f"{DAY}T01:10:00", f"{DAY}T06:50:00", 340, raw_id="sleep-1")],
        notifications=[notification_item(2, "카카오톡", "메시지", posted=f"{DAY}T02:32:00", raw_id="notif-1")],
    )
    draft = _draft(
        _event("event-001", "02:32", "07:52", (EventSourceType.NOTIFICATION, "notif-1"), title="아침부터 이어진 연락"),
        _event("event-002", "01:10", "06:50", SLEEP_REF, event_type=EventType.SLEEP, title="수면"),
    )

    repair_draft(draft, request)

    assert _titles(draft) == ["수면", "아침부터 이어진 연락"]
    morning = draft.events[1]
    assert morning.start_time.isoformat() == _t("06:50")
    # 수면 ↔ 아침 event 의 가짜 충돌 경고가 남지 않는다.
    assert not any("서로 겹치는 event" in w.message for w in draft.warnings)
