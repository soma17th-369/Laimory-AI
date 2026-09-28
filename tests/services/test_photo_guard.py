"""사진 귀속 검사와 강제 (#56 §7.3, #119).

사진은 사용자가 직접 골라 넣은 입력이라 최종 결과에서 사라지면 곧바로 알아챈다.
계약은 둘이다 — 정상 처리된 사진은 **정확히 하나의** event 에만 속하고, 하나의 event 에는
여러 사진이 함께 속할 수 있다(N:1).

`verify_photo_assignment` 는 검출만 하고 `enforce_photo_assignment` 는 항상 그 상태로
만든다. 앞쪽 테스트는 검출을, 뒤쪽 테스트는 강제를 본다.
"""

import pytest

from app.schemas import EventType, TimelineDraft
from app.services.photo_guard import (
    enforce_photo_assignment,
    inspect_photo_assignment,
    verify_photo_assignment,
)
from app.services.timeline_validator import validate_timeline_for_storage
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    photo_item,
    stay_item,
)

PHOTO_1 = fixture_raw_id("photo-1")
PHOTO_2 = fixture_raw_id("photo-2")
STAY_1 = fixture_raw_id("stay-1")


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


def _event(client_event_id: str, photo_raw_ids: list[str], *, hour: int = 12) -> dict:
    return {
        "clientEventId": client_event_id,
        "eventType": "PHOTO_MOMENT",
        "title": "사진",
        "description": "",
        "startTime": f"2026-06-20T{hour:02d}:00:00+09:00",
        "endTime": f"2026-06-20T{hour:02d}:30:00+09:00",
        "confidence": 0.8,
        "inferenceLevel": "EVIDENCE_BASED",
        "sourceRefs": [
            {"sourceType": "PHOTO", "rawId": raw_id} for raw_id in photo_raw_ids
        ],
        "uncertainty": [],
    }


def _request():
    # photo_item 은 id 앞에 "photo-" 를 붙여 rawId 를 만든다.
    return make_request(photos=[photo_item("1"), photo_item("2")])


def test_every_photo_in_exactly_one_event_is_clean():
    draft = _draft([_event("event-001", [PHOTO_1]), _event("event-002", [PHOTO_2])])

    verify_photo_assignment(draft, _request())

    assert draft.warnings == []


def test_several_photos_may_share_one_event():
    """N:1 은 정상이다. 같은 사건을 보여 주는 사진은 한 event 에 묶인다."""

    draft = _draft([_event("event-001", [PHOTO_1, PHOTO_2])])

    verify_photo_assignment(draft, _request())

    assert draft.warnings == []


def test_missing_photo_is_warned():
    draft = _draft([_event("event-001", [PHOTO_1])])

    assignment = verify_photo_assignment(draft, _request())

    assert assignment.missing == {PHOTO_2}
    assert len(draft.warnings) == 1
    assert "어느 event 에도 연결되지 않았습니다" in draft.warnings[0].message


def test_duplicated_photo_is_warned():
    draft = _draft(
        [
            _event("event-001", [PHOTO_1, PHOTO_2]),
            _event("event-002", [PHOTO_1], hour=15),
        ]
    )

    assignment = verify_photo_assignment(draft, _request())

    assert set(assignment.duplicated) == {PHOTO_1}
    assert assignment.duplicated[PHOTO_1] == ["event-001", "event-002"]
    assert any("여러 event 에" in w.message for w in draft.warnings)


def test_verify_does_not_modify_events():
    """검출은 고치지 않는다. 고치는 것은 `enforce_photo_assignment` 다."""

    draft = _draft(
        [_event("event-001", [PHOTO_1]), _event("event-002", [PHOTO_1], hour=15)]
    )
    before = [list(event.source_refs) for event in draft.events]

    verify_photo_assignment(draft, _request())

    assert [list(event.source_refs) for event in draft.events] == before


def test_no_photos_in_request_is_a_no_op():
    draft = _draft([])

    assignment = verify_photo_assignment(draft, make_request())

    assert assignment.input_raw_ids == set()
    assert draft.warnings == []


def test_inspect_ignores_non_photo_source_refs():
    """사진이 아닌 근거는 단일 귀속 계약의 대상이 아니다."""

    event = _event("event-001", [PHOTO_1])
    event["sourceRefs"].append(
        {"sourceType": "STAY", "rawId": fixture_raw_id("stay-1")}
    )
    draft = _draft([event, _event("event-002", [PHOTO_2], hour=15)])

    assignment = inspect_photo_assignment(draft, _request())

    assert assignment.missing == set()
    assert assignment.duplicated == {}


# --- 강제 (#119) ----------------------------------------------------------------
#
# 아래 테스트가 지키는 것은 하나다. 강제를 지난 draft 에서 **입력의 모든 사진은 정확히 한
# event 에만 있고, 근거가 없는 event 는 없다.**


def _timed_event(
    client_event_id: str,
    refs: list[tuple[str, str]],
    *,
    start: str,
    end: str,
    event_type: str = "PHOTO_MOMENT",
    confidence: float = 0.8,
    title: str | None = None,
) -> dict:
    return {
        "clientEventId": client_event_id,
        "eventType": event_type,
        "title": title or client_event_id,
        "description": "",
        "startTime": f"2026-06-20T{start}:00+09:00",
        "endTime": f"2026-06-20T{end}:00+09:00",
        "confidence": confidence,
        "inferenceLevel": "EVIDENCE_BASED",
        "sourceRefs": [
            {"sourceType": source_type, "rawId": raw_id} for source_type, raw_id in refs
        ],
        "uncertainty": [],
    }


def _photo_request(*taken: str):
    """사진을 찍은 시각대로 1번부터 만든다."""

    return make_request(
        photos=[
            photo_item(str(index), taken=f"2026-06-20T{clock}:00")
            for index, clock in enumerate(taken, start=1)
        ],
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T18:00:00",
                place="카페",
            )
        ],
    )


def _holders(draft: TimelineDraft, raw_id: str) -> list[str]:
    return [
        event.client_event_id
        for event in draft.events
        if any(ref.raw_id == raw_id for ref in event.source_refs)
    ]


def assert_every_photo_is_in_exactly_one_event(draft: TimelineDraft, request) -> None:
    assignment = inspect_photo_assignment(draft, request)
    assert assignment.missing == set()
    assert assignment.duplicated == {}
    for photo in request.photos:
        assert len(_holders(draft, photo.raw_id)) == 1
    assert all(event.source_refs for event in draft.events)


def test_event_with_other_evidence_keeps_the_photo_and_the_photo_card_goes():
    """같은 사진을 식사 event 와 사진 event 가 함께 가지면 식사가 남는다.

    체류와 함께 사진을 가진 event 는 그 사진으로 무엇을 했는지 말하는 event 이고, 사진만으로
    만든 event 는 같은 사진을 한 번 더 보여 주는 카드다.
    """

    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event(
                "meal",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="12:20",
                end="12:50",
                event_type="MEAL",
            ),
            _timed_event("moment", [("PHOTO", PHOTO_1)], start="12:30", end="12:30"),
        ]
    )

    enforcement = enforce_photo_assignment(draft, request)

    assert [event.client_event_id for event in draft.events] == ["meal"]
    assert [event.client_event_id for event in enforcement.removed] == ["moment"]
    assert enforcement.deduplicated == [PHOTO_1]
    assert_every_photo_is_in_exactly_one_event(draft, request)


def test_evidence_comes_before_capture_time():
    """다른 근거가 있는 event 가 촬영 시각을 포함하지 않아도 먼저다."""

    request = _photo_request("12:55")
    draft = _draft(
        [
            _timed_event(
                "meal",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="12:00",
                end="12:50",
                event_type="MEAL",
            ),
            _timed_event("moment", [("PHOTO", PHOTO_1)], start="12:55", end="12:55"),
        ]
    )

    enforce_photo_assignment(draft, request)

    assert _holders(draft, PHOTO_1) == ["meal"]


def test_among_events_with_other_evidence_the_one_containing_the_capture_wins():
    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event(
                "morning",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="09:00",
                end="11:00",
                event_type="WORK",
            ),
            _timed_event(
                "lunch",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="12:20",
                end="12:50",
                event_type="MEAL",
            ),
        ]
    )

    enforce_photo_assignment(draft, request)

    assert _holders(draft, PHOTO_1) == ["lunch"]
    assert len(draft.events) == 2  # 다른 근거가 남은 event 는 지우지 않는다


def test_among_containing_events_the_shortest_wins():
    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event(
                "background",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="09:00",
                end="18:00",
                event_type="WORK",
            ),
            _timed_event(
                "lunch",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="12:20",
                end="12:50",
                event_type="MEAL",
            ),
        ]
    )

    enforce_photo_assignment(draft, request)

    assert _holders(draft, PHOTO_1) == ["lunch"]


def test_duplicate_photo_cards_collapse_into_one():
    """사진만 근거인 event 끼리 같은 사진을 가지면 하나만 남는다."""

    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event("far", [("PHOTO", PHOTO_1)], start="15:00", end="15:00"),
            _timed_event("near", [("PHOTO", PHOTO_1)], start="12:30", end="12:30"),
        ]
    )

    enforce_photo_assignment(draft, request)

    assert [event.client_event_id for event in draft.events] == ["near"]
    assert_every_photo_is_in_exactly_one_event(draft, request)


def test_photo_card_with_another_photo_survives():
    """사진 하나를 뺏겨도 다른 사진이 남아 있으면 event 는 남는다."""

    request = _photo_request("12:30", "12:31")
    draft = _draft(
        [
            _timed_event(
                "meal",
                [("STAY", STAY_1), ("PHOTO", PHOTO_1)],
                start="12:20",
                end="12:50",
                event_type="MEAL",
            ),
            _timed_event(
                "moment",
                [("PHOTO", PHOTO_1), ("PHOTO", PHOTO_2)],
                start="12:30",
                end="12:31",
            ),
        ]
    )

    enforcement = enforce_photo_assignment(draft, request)

    assert enforcement.removed == []
    assert _holders(draft, PHOTO_1) == ["meal"]
    assert _holders(draft, PHOTO_2) == ["moment"]
    assert_every_photo_is_in_exactly_one_event(draft, request)


def test_missing_photo_goes_to_the_event_containing_its_capture_time():
    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event("morning", [("STAY", STAY_1)], start="09:00", end="11:00", event_type="WORK"),
            _timed_event("lunch", [("STAY", STAY_1)], start="12:20", end="12:50", event_type="MEAL"),
        ]
    )

    enforcement = enforce_photo_assignment(draft, request)

    assert enforcement.attached == [PHOTO_1]
    assert _holders(draft, PHOTO_1) == ["lunch"]
    attached = draft.events[1].source_refs[-1]
    assert attached.source_type.value == "PHOTO"
    assert "안에 있다" in attached.reason


def test_missing_photo_goes_to_the_nearest_event_when_none_contains_it():
    request = _photo_request("20:00")
    draft = _draft(
        [
            _timed_event("morning", [("STAY", STAY_1)], start="09:00", end="11:00", event_type="WORK"),
            _timed_event("evening", [("STAY", STAY_1)], start="17:00", end="18:00", event_type="REST"),
        ]
    )

    enforce_photo_assignment(draft, request)

    assert _holders(draft, PHOTO_1) == ["evening"]
    assert "가장 가까운" in draft.events[1].source_refs[-1].reason


def test_without_any_event_a_photo_moment_is_created():
    """담을 event 가 없어도 사진을 버리지 않는다."""

    request = _photo_request("12:30", "20:00")
    draft = _draft([])

    enforcement = enforce_photo_assignment(draft, request)

    assert len(enforcement.created) == 2
    assert [event.event_type for event in draft.events] == [EventType.PHOTO_MOMENT] * 2
    assert sorted(event.start_time.hour for event in draft.events) == [12, 20]
    assert all(event.start_time == event.end_time for event in draft.events)
    assert_every_photo_is_in_exactly_one_event(draft, request)


def test_created_event_stays_inside_the_window():
    request = make_request(photos=[photo_item("1", taken="2026-06-19T23:00:00")])
    draft = _draft([])

    enforce_photo_assignment(draft, request)

    assert draft.events[0].start_time.isoformat() == "2026-06-20T00:00:00+09:00"


def test_unreadable_capture_time_still_gets_an_event():
    request = make_request(photos=[photo_item("1", taken="알 수 없음")])
    draft = _draft([])

    enforce_photo_assignment(draft, request)

    assert draft.events[0].start_time.isoformat() == "2026-06-20T00:00:00+09:00"
    assert_every_photo_is_in_exactly_one_event(draft, request)


def test_unreadable_capture_time_is_attached_when_events_exist():
    request = make_request(
        photos=[photo_item("1", taken="알 수 없음")],
        stays=[stay_item(1, raw_id="stay-1")],
    )
    draft = _draft(
        [_timed_event("only", [("STAY", STAY_1)], start="09:00", end="10:00", event_type="REST")]
    )

    enforce_photo_assignment(draft, request)

    assert _holders(draft, PHOTO_1) == ["only"]


def test_same_photo_twice_in_one_event_is_reduced_to_one():
    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event(
                "moment",
                [("PHOTO", PHOTO_1), ("PHOTO", PHOTO_1)],
                start="12:30",
                end="12:30",
            )
        ]
    )

    enforce_photo_assignment(draft, request)

    assert [ref.raw_id for ref in draft.events[0].source_refs] == [PHOTO_1]


def test_photo_is_recognized_by_raw_id_not_by_label():
    """LLM 이 사진 참조에 `STAY` 라고 적어도 빠져나가지 못한다."""

    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event("a", [("STAY", STAY_1), ("STAY", PHOTO_1)], start="12:20", end="12:50", event_type="MEAL"),
            _timed_event("b", [("STAY", STAY_1), ("PHOTO", PHOTO_1)], start="09:00", end="18:00", event_type="WORK"),
        ]
    )

    enforce_photo_assignment(draft, request)

    assert _holders(draft, PHOTO_1) == ["a"]


def test_several_photos_may_still_share_one_event():
    request = _photo_request("12:30", "12:31")
    draft = _draft(
        [
            _timed_event(
                "moment",
                [("PHOTO", PHOTO_1), ("PHOTO", PHOTO_2)],
                start="12:30",
                end="12:31",
            )
        ]
    )

    enforcement = enforce_photo_assignment(draft, request)

    assert not enforcement.acted
    assert draft.warnings == []


def test_enforcement_is_idempotent():
    request = _photo_request("12:30", "20:00")
    draft = _draft(
        [
            _timed_event("meal", [("STAY", STAY_1), ("PHOTO", PHOTO_1)], start="12:20", end="12:50", event_type="MEAL"),
            _timed_event("moment", [("PHOTO", PHOTO_1)], start="12:30", end="12:30"),
        ]
    )

    enforce_photo_assignment(draft, request)
    first = draft.model_copy(deep=True)
    second = enforce_photo_assignment(draft, request)

    assert not second.acted
    assert draft.events == first.events


def test_enforced_draft_passes_storage_validation():
    """근거 없는 event 가 남으면 저장 전 검증이 하루 전체를 실패시킨다."""

    request = _photo_request("12:30")
    draft = _draft(
        [
            _timed_event("meal", [("STAY", STAY_1), ("PHOTO", PHOTO_1)], start="12:20", end="12:50", event_type="MEAL"),
            _timed_event("moment", [("PHOTO", PHOTO_1)], start="12:30", end="12:30"),
        ]
    )

    enforce_photo_assignment(draft, request)

    valid = {item.raw_id for item in request.iter_source_items()}
    assert validate_timeline_for_storage(draft, valid) == []


def test_request_without_photos_is_left_alone():
    draft = _draft(
        [_timed_event("only", [("STAY", STAY_1)], start="09:00", end="10:00", event_type="REST")]
    )
    before = draft.model_copy(deep=True)

    enforcement = enforce_photo_assignment(draft, make_request())

    assert not enforcement.acted
    assert draft == before


@pytest.mark.parametrize("what", ["deduplicated", "attached"])
def test_enforcement_leaves_a_warning_about_what_it_did(what):
    request = _photo_request("12:30")
    events = {
        "deduplicated": [
            _timed_event("meal", [("STAY", STAY_1), ("PHOTO", PHOTO_1)], start="12:20", end="12:50", event_type="MEAL"),
            _timed_event("moment", [("PHOTO", PHOTO_1)], start="12:30", end="12:30"),
        ],
        "attached": [
            _timed_event("meal", [("STAY", STAY_1)], start="12:20", end="12:50", event_type="MEAL"),
        ],
    }[what]
    draft = _draft(events)

    enforce_photo_assignment(draft, request)

    (warning,) = draft.warnings
    assert warning.warning_id.startswith("warning-photo-enforced-")
    # rawId 는 warning 문장에 싣지 않는다.
    assert PHOTO_1 not in warning.message
