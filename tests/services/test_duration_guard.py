"""eventType 별 지속시간 상한 검사 (#61, #119).

프롬프트는 event 를 타입마다 정해진 길이 이내로 만들라고 지시한다. 그 지시를 지켰는지
재는 코드가 없으면 하루가 event 하나로 뭉개져도 결과를 볼 때까지 모른다.

**재기만 한다.** 어디서 끊을지는 의미 판단이라 코드가 정하지 않고 Repair 가
`OVEREXTENDED_EVENT` 로 처리한다. 그래서 이 테스트는 event 가 잘리거나 나뉘지 않는 것도
함께 확인한다.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.schemas import EventType, TimelineDraft, TimelineWarningSeverity
from app.services.duration_guard import (
    DURATION_LIMITS,
    LEGACY_MAX_EVENT_DURATION,
    max_duration_for,
    verify_event_duration,
)
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


# 상한 값은 `DURATION_LIMITS` 표 하나가 정한다. 이 파일은 값을 다시 적지 않는다. 경계는 표에서
# 읽고, "상한을 넘긴 event" 가 필요한 곳은 어떤 상한보다도 긴 event 를 쓴다. 값을 바꿔도
# 테스트를 고치지 않아야 하고, 같은 값을 말해야 하는 프롬프트와 문서가 따라왔는지만 본다.

#: 어떤 상한보다도 긴 event 의 시작·종료.
LONG_START = "01:00:00"
LONG_END = "23:00:00"
_LONG = timedelta(hours=22)


def _long_event(**overrides) -> dict:
    return _event(start=LONG_START, end=LONG_END, **overrides)


def test_long_fixture_exceeds_every_limit():
    """아래 테스트들의 전제다. 상한을 22시간 이상으로 올리면 fixture 부터 고친다."""

    limits = [limit for limit in DURATION_LIMITS.values() if limit is not None]

    assert all(limit < _LONG for limit in [*limits, LEGACY_MAX_EVENT_DURATION])


def test_warning_names_the_event_and_carries_its_evidence():
    draft = _draft([_long_event()])

    verify_event_duration(draft)

    warnings = _duration_warnings(draft)
    assert len(warnings) == 1
    assert warnings[0].severity is TimelineWarningSeverity.LOW
    assert "마포에서 보낸 하루" in warnings[0].message
    assert [ref.raw_id for ref in warnings[0].source_refs] == [STAY_1]


def test_guard_does_not_modify_event_times():
    # 자르거나 나누지 않는다. 분할 판단은 Repair 몫이다.
    draft = _draft([_long_event()])
    before = (draft.events[0].start_time, draft.events[0].end_time)

    verify_event_duration(draft)

    assert (draft.events[0].start_time, draft.events[0].end_time) == before
    assert len(draft.events) == 1


def test_repeated_runs_do_not_accumulate():
    draft = _draft([_long_event()])

    verify_event_duration(draft)
    verify_event_duration(draft)

    assert len(_duration_warnings(draft)) == 1


def test_warning_disappears_after_repair_shortens_event():
    draft = _draft([_long_event()])
    verify_event_duration(draft)
    assert _duration_warnings(draft)

    # Repair 가 update_event 로 시간을 줄인 상황.
    draft.events[0].end_time = draft.events[0].start_time + timedelta(minutes=1)
    verify_event_duration(draft)

    assert _duration_warnings(draft) == []


# --- 타입별 상한 (#119) ----------------------------------------------------------


_LIMITED = sorted(
    (event_type for event_type, limit in DURATION_LIMITS.items() if limit is not None),
    key=lambda event_type: event_type.value,
)
_UNLIMITED = sorted(
    (event_type for event_type, limit in DURATION_LIMITS.items() if limit is None),
    key=lambda event_type: event_type.value,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _event_lasting(event_type: EventType, duration: timedelta) -> dict:
    start = datetime(2026, 6, 20, 0, 0, 0)
    end = start + duration
    return _event(
        event_type=event_type.value,
        start=start.strftime("%H:%M:%S"),
        end=end.strftime("%H:%M:%S"),
    )


def test_table_lists_every_event_type():
    """기본값이 없다. 새 eventType 은 표에 적어야 하고, 빠뜨리면 import 에서 멈춘다."""

    assert set(DURATION_LIMITS) == set(EventType)
    assert all(max_duration_for(event_type) == DURATION_LIMITS[event_type] for event_type in EventType)


@pytest.mark.parametrize("event_type", _LIMITED, ids=lambda t: t.value)
def test_each_type_is_measured_at_its_own_limit(event_type):
    limit = DURATION_LIMITS[event_type]

    at_limit = _draft([_event_lasting(event_type, limit)])
    over_limit = _draft([_event_lasting(event_type, limit + timedelta(minutes=1))])

    assert verify_event_duration(at_limit) == []
    (finding,) = verify_event_duration(over_limit)
    assert finding.limit == limit
    assert len(_duration_warnings(over_limit)) == 1


@pytest.mark.parametrize("event_type", _UNLIMITED, ids=lambda t: t.value)
def test_types_without_a_limit_are_never_measured(event_type):
    """지속 구간이 근거에 직접 있거나 다른 guard 가 맡는 종류다.

    `MEAL` 은 `meal_guard` 가 20~60분으로 이미 전담하므로 여기서 두 번 경고하지 않는다.
    """

    draft = _draft([_long_event(event_type=event_type.value)])

    assert verify_event_duration(draft) == []
    assert verify_event_duration(draft, by_type=False) == []


def _hours_text(limit: timedelta) -> str:
    hours = limit.total_seconds() / 3600
    return f"{hours:.1f}".removesuffix(".0")


@pytest.mark.parametrize("event_type", _LIMITED, ids=lambda t: t.value)
def test_timeline_v3_prompt_states_the_same_limit(event_type):
    """프롬프트가 다른 값을 말하면 Timeline 이 다시 보는 길이와 코드가 짚는 길이가 갈린다.

    값은 자르는 상한이 아니라 검토 기준이다(#134). 프롬프트가 "최대" 라고 적으면 Timeline 이
    그 길이에 맞춰 나눈다.
    """

    prompt = (
        _REPO_ROOT / "app/agents/timeline/prompts/v3/timeline.md"
    ).read_text(encoding="utf-8")
    section = prompt.split(f"### `{event_type.value}` — ", 1)[1].split("\n### ", 1)[0]
    (time_line,) = [line for line in section.splitlines() if line.startswith("- **시간**")]

    assert re.findall(r"검토 기준 ([\d.]+)시간", time_line) == [
        _hours_text(DURATION_LIMITS[event_type])
    ]
    assert "최대" not in time_line


def test_document_table_states_the_same_limits():
    """`docs/ai-event-candidate.md` 의 표가 코드와 같은 값을 말하는가."""

    document = (_REPO_ROOT / "docs/ai-event-candidate.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `(\w+)` \| [^|]+ \| ([^|]+) \|$", document, re.M))

    for event_type in _LIMITED:
        stated = re.findall(r"검토 기준 ([\d.]+)시간", rows[event_type.value])
        assert stated == [_hours_text(DURATION_LIMITS[event_type])], event_type.value
    for event_type in _UNLIMITED:
        assert "검토 기준" not in rows[event_type.value], event_type.value


def test_finding_carries_type_duration_and_limit():
    limit = DURATION_LIMITS[EventType.MEETING]
    duration = limit + timedelta(minutes=90)
    draft = _draft([_event_lasting(EventType.MEETING, duration)])

    (finding,) = verify_event_duration(draft)

    assert finding.event is draft.events[0]
    assert finding.detail() == {
        "eventType": "MEETING",
        "durationHours": _hours_text(duration),
        "limitHours": _hours_text(limit),
    }
    assert f"MEETING 검토 기준 {_hours_text(limit)}시간" in _duration_warnings(draft)[0].message


def test_v3_warning_asks_to_review_not_to_split():
    """#119 문장은 "나눠야 합니다" 였고, Repair 는 경계 없는 체류를 고르게 쪼갰다(#134)."""

    draft = _draft([_long_event()])

    verify_event_duration(draft)

    message = _duration_warnings(draft)[0].message
    assert "나눠야" not in message
    assert "묻혀 있는지 확인하세요" in message
    assert "길다는 이유만으로 나누지 않습니다" in message


# --- 면제 (#119) ----------------------------------------------------------------


def _calendar_request(start: str = LONG_START, end: str = LONG_END):
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
    """일정이 하루 종일이면 그 시간을 따르는 event 는 길어도 일정대로다."""

    draft = _draft(
        [
            _long_event(
                event_type=event_type,
                refs=[("CALENDAR", CALENDAR_1), ("STAY", STAY_1)],
            )
        ]
    )

    assert verify_event_duration(draft, _calendar_request()) == []
    assert _duration_warnings(draft) == []


def test_event_longer_than_its_calendar_is_not_exempt():
    """1시간짜리 일정을 근거로 댄 하루 종일 event 는 일정대로가 아니다."""

    draft = _draft(
        [_long_event(event_type="WORK", refs=[("CALENDAR", CALENDAR_1)])]
    )

    findings = verify_event_duration(
        draft, _calendar_request(start="10:00:00", end="11:00:00")
    )

    assert len(findings) == 1


def test_calendar_exemption_needs_the_request():
    """일정의 길이를 모르면 면제할 근거가 없다."""

    draft = _draft(
        [_long_event(event_type="WORK", refs=[("CALENDAR", CALENDAR_1)])]
    )

    assert len(verify_event_duration(draft)) == 1


def test_walk_keeps_its_round_trip_span():
    """이동을 근거로 댄 `EXERCISE` 는 산책이다. 왕복 구간을 그대로 쓴다."""

    request = make_request(
        movements=[
            movement_item(
                1,
                raw_id="movement-1",
                start=f"2026-06-20T{LONG_START}",
                end=f"2026-06-20T{LONG_END}",
            )
        ]
    )
    draft = _draft(
        [_long_event(event_type="EXERCISE", refs=[("MOVEMENT", MOVEMENT_1)])]
    )

    assert verify_event_duration(draft, request) == []


# --- 코드가 합치는 체류 (#119) ---------------------------------------------------
#
# 이동 없이 같은 장소에서 이어진 체류는 확정 pass 가 하나로 합친다. 나눠도 다음 확정에서
# 다시 합쳐지므로 Repair 에 나누라고 알리지 않는다.


def _fragmented_stay_request(*, with_movement_between: bool = False):
    """같은 장소의 STAY 가 하루의 처음과 끝에 몇 분짜리 조각으로 들어온 입력."""

    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start=f"2026-06-20T{LONG_START}",
                end="2026-06-20T01:10:00",
                place="오산운암3단지 주공아파트",
            ),
            stay_item(
                2,
                raw_id="stay-2",
                start="2026-06-20T22:50:00",
                end=f"2026-06-20T{LONG_END}",
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
    return _long_event(event_type="UNKNOWN", refs=refs)


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
                start=f"2026-06-20T{LONG_START}",
                end=f"2026-06-20T{LONG_END}",
                place="회사",
            )
        ]
    )
    draft = _draft([_long_event(event_type="WORK")])

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


def test_legacy_limit_is_the_one_the_old_prompts_were_written_for():
    """v1·v2 프롬프트는 "3시간 이내"라고 적는다. 운영 세트라 이 값은 바꾸지 않는다."""

    assert LEGACY_MAX_EVENT_DURATION == timedelta(hours=3)


@pytest.mark.parametrize("event_type", _LIMITED, ids=lambda t: t.value)
def test_legacy_sets_measure_every_type_at_the_same_limit(event_type):
    at_limit = _draft([_event_lasting(event_type, LEGACY_MAX_EVENT_DURATION)])
    over_limit = _draft(
        [_event_lasting(event_type, LEGACY_MAX_EVENT_DURATION + timedelta(minutes=1))]
    )

    assert verify_event_duration(at_limit, by_type=False) == []
    (finding,) = verify_event_duration(over_limit, by_type=False)
    assert finding.limit == LEGACY_MAX_EVENT_DURATION


def test_legacy_sets_keep_the_old_warning_sentence():
    draft = _draft([_long_event(event_type="MEETING")])

    verify_event_duration(draft, by_type=False)

    limit = _hours_text(LEGACY_MAX_EVENT_DURATION)
    message = _duration_warnings(draft)[0].message
    assert f"비캘린더 event 권장 상한 {limit}시간" in message
    # v1·v2 는 운영 세트다. #134 의 문장 변경이 새어 들면 안 된다.
    assert message.endswith("하나의 활동이 계속됐다는 근거가 없으면 나눠야 합니다.")


def test_legacy_sets_have_no_calendar_exemption():
    draft = _draft(
        [_long_event(event_type="WORK", refs=[("CALENDAR", CALENDAR_1)])]
    )

    assert len(verify_event_duration(draft, _calendar_request(), by_type=False)) == 1


def test_exercise_without_movement_evidence_is_not_a_walk():
    draft = _draft([_long_event(event_type="EXERCISE")])

    assert len(verify_event_duration(draft)) == 1
