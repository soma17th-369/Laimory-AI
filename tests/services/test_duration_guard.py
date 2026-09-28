"""eventType 별 지속시간 상한 검사 (#61, #119).

프롬프트는 event 를 타입마다 정해진 길이 이내로 만들라고 지시한다. 그 지시를 지켰는지
재는 코드가 없으면 하루가 event 하나로 뭉개져도 결과를 볼 때까지 모른다.

**재기만 한다.** 어디서 끊을지는 의미 판단이라 코드가 정하지 않고 Repair 가
`OVEREXTENDED_EVENT` 로 처리한다. 그래서 이 테스트는 event 가 잘리거나 나뉘지 않는 것도
함께 확인한다.
"""

from datetime import timedelta

import pytest

from app.schemas import EventType, TimelineDraft, TimelineWarningSeverity
from app.services.duration_guard import max_duration_for, verify_event_duration
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    movement_item,
    stay_item,
)

STAY_1 = fixture_raw_id("stay-1")
STAY_2 = fixture_raw_id("stay-2")
CALENDAR_1 = fixture_raw_id("calendar-1")
MOVEMENT_1 = fixture_raw_id("movement-1")


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


def _event(
    *,
    event_type: str = "WORK",
    start: str = "09:00:00",
    end: str = "12:00:00",
    title: str = "마포에서 보낸 하루",
    refs: list[tuple[str, str]] | None = None,
) -> dict:
    return {
        "clientEventId": "event-001",
        "eventType": event_type,
        "title": title,
        "description": "오전부터 마포에 머물렀어요.",
        "startTime": f"2026-06-20T{start}+09:00",
        "endTime": f"2026-06-20T{end}+09:00",
        "confidence": 0.7,
        "inferenceLevel": "INFERRED",
        "sourceRefs": [
            {"sourceType": source_type, "rawId": raw_id, "reason": "근거"}
            for source_type, raw_id in (refs or [("STAY", STAY_1)])
        ],
    }


def _duration_warnings(draft: TimelineDraft) -> list:
    return [w for w in draft.warnings if w.warning_id.startswith("warning-event-duration-")]


def test_exactly_three_hours_is_not_warned():
    draft = _draft([_event(start="09:00:00", end="12:00:00")])

    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


def test_over_three_hours_is_warned():
    draft = _draft([_event(start="09:00:00", end="12:01:00")])

    verify_event_duration(draft)

    warnings = _duration_warnings(draft)
    assert len(warnings) == 1
    assert warnings[0].severity is TimelineWarningSeverity.LOW
    assert "마포에서 보낸 하루" in warnings[0].message
    assert [ref.raw_id for ref in warnings[0].source_refs] == [STAY_1]


def test_guard_does_not_modify_event_times():
    # 자르거나 나누지 않는다. 분할 판단은 Repair 몫이다.
    draft = _draft([_event(start="09:00:00", end="21:00:00")])
    before = (draft.events[0].start_time, draft.events[0].end_time)

    verify_event_duration(draft)

    assert (draft.events[0].start_time, draft.events[0].end_time) == before
    assert len(draft.events) == 1


@pytest.mark.parametrize(
    "event_type", ["CALENDAR_EVENT", "SLEEP", "MOVEMENT", "MEAL"]
)
def test_exempt_event_types_are_not_warned(event_type):
    """지속 구간이 근거에 직접 있는 종류는 상한을 적용하지 않는다.

    `MEAL` 은 `meal_guard` 가 20~60분으로 이미 전담하므로 여기서 두 번 경고하지 않는다.
    """

    draft = _draft([_event(event_type=event_type, start="09:00:00", end="21:00:00")])

    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


def test_repeated_runs_do_not_accumulate():
    draft = _draft([_event(start="09:00:00", end="21:00:00")])

    verify_event_duration(draft)
    verify_event_duration(draft)

    assert len(_duration_warnings(draft)) == 1


def test_warning_disappears_after_repair_shortens_event():
    draft = _draft([_event(start="09:00:00", end="21:00:00")])
    verify_event_duration(draft)
    assert _duration_warnings(draft)

    # Repair 가 update_event 로 시간을 줄인 상황.
    draft.events[0].end_time = draft.events[0].start_time.replace(hour=11)
    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


# --- 타입별 상한 (#119) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "hours"),
    [
        (EventType.PHOTO_MOMENT, 1),
        (EventType.MEETING, 2),
        (EventType.EXERCISE, 2),
        (EventType.CLASS, 3),
        (EventType.WORK, 3),
        (EventType.SOCIAL, 3),
        (EventType.REST, 3),
        (EventType.UNKNOWN, 3),
    ],
)
def test_each_type_has_the_limit_the_prompt_states(event_type, hours):
    """상한은 Timeline v3 프롬프트와 `docs/ai-event-candidate.md` 표의 값이다."""

    assert max_duration_for(event_type) == timedelta(hours=hours)


@pytest.mark.parametrize(
    ("event_type", "end", "warned"),
    [
        ("PHOTO_MOMENT", "10:00:00", False),
        ("PHOTO_MOMENT", "10:01:00", True),
        ("MEETING", "11:00:00", False),
        ("MEETING", "11:01:00", True),
        ("EXERCISE", "11:00:00", False),
        ("EXERCISE", "11:01:00", True),
    ],
)
def test_shorter_limits_are_measured_at_their_own_boundary(event_type, end, warned):
    draft = _draft([_event(event_type=event_type, start="09:00:00", end=end)])

    findings = verify_event_duration(draft)

    assert bool(findings) is warned
    assert bool(_duration_warnings(draft)) is warned


def test_finding_carries_type_duration_and_limit():
    draft = _draft([_event(event_type="MEETING", start="09:00:00", end="12:30:00")])

    (finding,) = verify_event_duration(draft)

    assert finding.event is draft.events[0]
    assert finding.detail() == {
        "eventType": "MEETING",
        "durationHours": "3.5",
        "limitHours": "2",
    }
    assert "MEETING 상한 2시간" in _duration_warnings(draft)[0].message


# --- 면제 (#119) ----------------------------------------------------------------


def _calendar_request(start: str = "09:00:00", end: str = "23:00:00"):
    return make_request(
        calendars=[
            calendar_item(
                1,
                "ASM 프로젝트 MVP 개발",
                start=f"2026-06-20T{start}",
                end=f"2026-06-20T{end}",
                raw_id="calendar-1",
            )
        ]
    )


@pytest.mark.parametrize("event_type", ["WORK", "SOCIAL", "MEETING", "CLASS", "EXERCISE"])
def test_event_that_follows_its_calendar_is_exempt(event_type):
    """일정이 09:00~23:00 이면 그 시간을 따르는 event 는 길어도 일정대로다."""

    draft = _draft(
        [
            _event(
                event_type=event_type,
                start="09:00:00",
                end="23:00:00",
                refs=[("CALENDAR", CALENDAR_1), ("STAY", STAY_1)],
            )
        ]
    )

    assert verify_event_duration(draft, _calendar_request()) == []
    assert _duration_warnings(draft) == []


def test_event_longer_than_its_calendar_is_not_exempt():
    """1시간짜리 일정을 근거로 댄 8시간 event 는 일정대로가 아니다."""

    draft = _draft(
        [
            _event(
                event_type="WORK",
                start="09:00:00",
                end="17:00:00",
                refs=[("CALENDAR", CALENDAR_1)],
            )
        ]
    )

    findings = verify_event_duration(
        draft, _calendar_request(start="10:00:00", end="11:00:00")
    )

    assert len(findings) == 1


def test_calendar_exemption_needs_the_request():
    """일정의 길이를 모르면 면제할 근거가 없다."""

    draft = _draft(
        [
            _event(
                event_type="WORK",
                start="09:00:00",
                end="23:00:00",
                refs=[("CALENDAR", CALENDAR_1)],
            )
        ]
    )

    assert len(verify_event_duration(draft)) == 1


def test_walk_keeps_its_round_trip_span():
    """이동을 근거로 댄 `EXERCISE` 는 산책이다. 왕복 구간을 그대로 쓴다."""

    request = make_request(
        movements=[
            movement_item(
                1,
                raw_id="movement-1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T12:00:00",
            )
        ]
    )
    draft = _draft(
        [
            _event(
                event_type="EXERCISE",
                start="09:00:00",
                end="12:00:00",
                refs=[("MOVEMENT", MOVEMENT_1)],
            )
        ]
    )

    assert verify_event_duration(draft, request) == []


# --- 코드가 합치는 체류 (#119) ---------------------------------------------------
#
# 이동 없이 같은 장소에서 이어진 체류는 확정 pass 가 하나로 합친다. 나눠도 다음 확정에서
# 다시 합쳐지므로 Repair 에 나누라고 알리지 않는다.


def _fragmented_stay_request(*, with_movement_between: bool = False):
    """같은 장소의 STAY 가 세 시간 간격으로 두 조각 들어온 입력."""

    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start="2026-06-20T11:12:00",
                end="2026-06-20T11:20:00",
                place="오산운암3단지 주공아파트",
            ),
            stay_item(
                2,
                raw_id="stay-2",
                start="2026-06-20T14:26:00",
                end="2026-06-20T14:36:00",
                place="오산운암3단지 주공아파트",
            ),
        ],
        movements=(
            [
                movement_item(
                    1,
                    raw_id="movement-1",
                    start="2026-06-20T12:00:00",
                    end="2026-06-20T12:30:00",
                )
            ]
            if with_movement_between
            else []
        ),
    )


def _merged_stay_event(refs: list[tuple[str, str]]) -> dict:
    return _event(event_type="UNKNOWN", start="11:12:00", end="14:36:00", refs=refs)


def test_stay_the_code_merges_is_exempt():
    """실제 LLM 은 3.4시간짜리 체류를 세 번 나눴고 세 번 다 도로 합쳐졌다."""

    draft = _draft([_merged_stay_event([("STAY", STAY_1), ("STAY", STAY_2)])])

    assert verify_event_duration(draft, _fragmented_stay_request()) == []
    assert _duration_warnings(draft) == []


def test_piece_of_a_merged_stay_is_exempt_too():
    """묶음의 일부만 참조해도 나눈 조각은 그 묶음의 체류라 다시 합쳐진다."""

    draft = _draft([_merged_stay_event([("STAY", STAY_1)])])

    assert verify_event_duration(draft, _fragmented_stay_request()) == []


def test_stay_with_other_evidence_is_not_exempt():
    """사진·알림이 섞인 event 는 코드가 합치지 않는다. 나누면 나뉜 채로 남는다."""

    draft = _draft(
        [
            _merged_stay_event(
                [("STAY", STAY_1), ("STAY", STAY_2), ("PHOTO", fixture_raw_id("photo-1"))]
            )
        ]
    )

    assert len(verify_event_duration(draft, _fragmented_stay_request())) == 1


def test_stays_with_a_movement_between_are_not_exempt():
    """사이에 이동이 있으면 하나의 체류가 아니라 코드가 합치지 않는다."""

    draft = _draft([_merged_stay_event([("STAY", STAY_1), ("STAY", STAY_2)])])

    findings = verify_event_duration(
        draft, _fragmented_stay_request(with_movement_between=True)
    )

    assert len(findings) == 1


def test_single_stay_is_not_exempt():
    """STAY 하나로 만든 긴 event 는 묶음이 아니다. 나누면 나뉜 채로 남는다."""

    request = make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T18:00:00",
                place="회사",
            )
        ]
    )
    draft = _draft([_event(event_type="WORK", start="09:00:00", end="18:00:00")])

    assert len(verify_event_duration(draft, request)) == 1


def test_legacy_sets_have_no_merged_stay_exemption():
    draft = _draft([_merged_stay_event([("STAY", STAY_1), ("STAY", STAY_2)])])

    findings = verify_event_duration(
        draft, _fragmented_stay_request(), by_type=False
    )

    assert len(findings) == 1


# --- 예전 세트 (#119) -----------------------------------------------------------
#
# 타입별 상한은 v3 프롬프트가 정한 값이다. 그 지시를 받은 적 없는 세트에는 예전처럼 잰다.


@pytest.mark.parametrize(
    ("event_type", "end", "warned"),
    [
        ("PHOTO_MOMENT", "11:30:00", False),
        ("MEETING", "12:00:00", False),
        ("EXERCISE", "12:00:00", False),
        ("MEETING", "12:01:00", True),
        ("WORK", "12:01:00", True),
    ],
)
def test_legacy_sets_are_measured_at_three_hours(event_type, end, warned):
    draft = _draft([_event(event_type=event_type, start="09:00:00", end=end)])

    findings = verify_event_duration(draft, by_type=False)

    assert bool(findings) is warned


def test_legacy_sets_keep_the_old_warning_sentence():
    draft = _draft([_event(event_type="MEETING", start="09:00:00", end="13:00:00")])

    verify_event_duration(draft, by_type=False)

    assert "비캘린더 event 권장 상한 3시간" in _duration_warnings(draft)[0].message


def test_legacy_sets_have_no_calendar_exemption():
    draft = _draft(
        [
            _event(
                event_type="WORK",
                start="09:00:00",
                end="23:00:00",
                refs=[("CALENDAR", CALENDAR_1)],
            )
        ]
    )

    assert len(verify_event_duration(draft, _calendar_request(), by_type=False)) == 1


def test_exercise_without_movement_evidence_is_not_a_walk():
    draft = _draft([_event(event_type="EXERCISE", start="09:00:00", end="12:00:00")])

    assert len(verify_event_duration(draft)) == 1
