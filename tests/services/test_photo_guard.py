"""사진 귀속 검사 (#56 §7.3).

사진은 사용자가 직접 골라 넣은 입력이라 최종 결과에서 사라지면 곧바로 알아챈다.
계약은 둘이다 — 정상 처리된 사진은 **정확히 하나의** event 에만 속하고, 하나의 event 에는
여러 사진이 함께 속할 수 있다(N:1).

여기서는 검출만 한다. 어느 event 가 그 사진의 주인인지는 의미 판단이라 Repair Agent 가
`update_event` 로 정한다.
"""

from app.schemas import TimelineDraft
from app.services.photo_guard import inspect_photo_assignment, verify_photo_assignment
from tests.fixtures.requests import fixture_raw_id, make_request, photo_item

PHOTO_1 = fixture_raw_id("photo-1")
PHOTO_2 = fixture_raw_id("photo-2")


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


def test_guard_does_not_modify_events():
    """자동 해소는 하지 않는다. 어느 event 를 남길지는 Repair Agent 가 정한다."""

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
