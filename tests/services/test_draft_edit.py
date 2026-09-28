"""draft event 수정·삭제·나누기(결정론) 검증.

Repair Agent 가 "이 event 를 이렇게 고쳐라" 라고 말했을 때 실제로 무엇이 바뀌고 무엇이
바뀌지 않는지를 본다. 핵심은 **지정한 필드에만 닿는다**는 것과, 잘못된 수정을 반쯤
적용하지 않는다는 것이다.
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
from app.services.draft_edit import (
    DraftEditError,
    delete_event,
    find_event,
    split_event,
    update_event,
)
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    movement_item,
    photo_item,
    stay_item,
)


def _event(client_event_id: str, title: str = "체류", start: str = "09:00", end: str = "10:00"):
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=EventType.REST,
        title=title,
        description="설명",
        place="카페",
        tags=["휴식"],
        start_time=f"2026-06-20T{start}:00+09:00",
        end_time=f"2026-06-20T{end}:00+09:00",
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id("s-1"),
            )
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=list(events),
    )


def test_update_event_touches_only_given_fields():
    draft = _draft(_event("event-001"))

    updated = update_event(draft, "event-001", {"endTime": "2026-06-20T14:00:00+09:00"})

    assert updated.end_time.hour == 14
    # 지정하지 않은 필드는 원래 값 그대로다.
    assert updated.title == "체류"
    assert updated.place == "카페"
    assert updated.tags == ["휴식"]
    assert [ref.raw_id for ref in updated.source_refs] == [fixture_raw_id("s-1")]


def test_update_event_keeps_client_event_id():
    draft = _draft(_event("event-001"))

    # id 는 정렬 결과에 맞춰 코드가 부여하는 값이라 LLM 이 바꿀 수 없다.
    updated = update_event(draft, "event-001", {"title": "카페에서 쉬었다"})

    assert updated.client_event_id == "event-001"
    assert draft.events[0].client_event_id == "event-001"


def test_update_event_rejects_unknown_field():
    draft = _draft(_event("event-001"))

    with pytest.raises(DraftEditError, match="바꿀 수 없는 필드"):
        update_event(draft, "event-001", {"eventId": "e-9"})


def test_update_event_rejects_invalid_value_without_touching_event():
    draft = _draft(_event("event-001"))

    # endTime < startTime 은 스키마가 막는다. 막힌 수정은 event 를 건드리지 않는다.
    with pytest.raises(DraftEditError, match="스키마 검증"):
        update_event(draft, "event-001", {"endTime": "2026-06-20T08:00:00+09:00"})

    assert draft.events[0].end_time.hour == 10


def test_update_event_reports_missing_event():
    draft = _draft(_event("event-001"))

    with pytest.raises(DraftEditError, match="event-999"):
        update_event(draft, "event-999", {"title": "x"})


def test_delete_event_removes_only_that_event():
    draft = _draft(_event("event-001"), _event("event-002", title="산책"))

    removed = delete_event(draft, "event-001")

    assert removed.title == "체류"
    assert [event.client_event_id for event in draft.events] == ["event-002"]


def test_delete_event_does_not_renumber_remaining_events():
    """한 계획 안의 다음 도구 호출이 가리키는 id 가 어긋나면 안 된다."""

    draft = _draft(_event("event-001"), _event("event-002"), _event("event-003"))

    delete_event(draft, "event-001")

    assert [event.client_event_id for event in draft.events] == ["event-002", "event-003"]
    assert find_event(draft, "event-003").title == "체류"


# --- 나누기 (#119) --------------------------------------------------------------

DAY = "2026-06-20"
OUT = fixture_raw_id("move-out")
BACK = fixture_raw_id("move-back")
OFFICE = fixture_raw_id("stay-office")
PHOTO = fixture_raw_id("photo-1")
CALENDAR = fixture_raw_id("calendar-1")


def _journey_request():
    """집을 나서 → 회사에 머물고 → 돌아온 하루."""

    return make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=f"{DAY}T08:20:00", end=f"{DAY}T09:00:00"),
            movement_item(2, raw_id="move-back", start=f"{DAY}T18:00:00", end=f"{DAY}T19:00:00"),
        ],
        stays=[
            stay_item(1, raw_id="stay-office", start=f"{DAY}T09:00:00", end=f"{DAY}T18:00:00", place="회사")
        ],
        photos=[photo_item("1", taken=f"{DAY}T12:30:00")],
        calendars=[
            calendar_item(1, "주간 회의", start=f"{DAY}T10:00:00", end=f"{DAY}T11:00:00", raw_id="calendar-1")
        ],
    )


def _journey_event(*refs: tuple[EventSourceType, str]) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id="event-002",
        event_type=EventType.MOVEMENT,
        title="회사에 다녀옴",
        description="회사에 다녀왔어요.",
        place="집",
        address="서울 어딘가",
        tags=["이동"],
        start_time=f"{DAY}T08:20:00+09:00",
        end_time=f"{DAY}T19:00:00+09:00",
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=source_type, raw_id=raw_id)
            for source_type, raw_id in refs
        ],
        question="어디 다녀오셨어요?",
    )


def _part(start: str, end: str, title: str, **fields) -> dict:
    return {
        "startTime": f"{DAY}T{start}:00+09:00",
        "endTime": f"{DAY}T{end}:00+09:00",
        "title": title,
        **fields,
    }


_LOCATION_REFS = (
    (EventSourceType.MOVEMENT, OUT),
    (EventSourceType.STAY, OFFICE),
    (EventSourceType.MOVEMENT, BACK),
)

_THREE_PARTS = [
    _part("08:20", "09:00", "회사로 출근", description="회사로 출근했어요."),
    _part("09:00", "18:00", "회사에서 근무", eventType="WORK", description="회사에서 일했어요."),
    _part("18:00", "19:00", "집으로 귀가", description="집으로 돌아왔어요."),
]


def _raw_ids(event: TimelineEventDraft) -> list[str]:
    return [ref.raw_id for ref in event.source_refs]


def test_split_event_gives_each_part_the_evidence_of_its_time():
    draft = _draft(_event("event-001"), _journey_event(*_LOCATION_REFS))

    pieces = split_event(draft, "event-002", _THREE_PARTS, _journey_request())

    assert [piece.title for piece in pieces] == ["회사로 출근", "회사에서 근무", "집으로 귀가"]
    assert [_raw_ids(piece) for piece in pieces] == [[OUT], [OFFICE], [BACK]]
    assert [piece.event_type for piece in pieces] == [
        EventType.MOVEMENT,
        EventType.WORK,
        EventType.MOVEMENT,
    ]


def test_split_event_replaces_the_original_in_place():
    draft = _draft(_event("event-001"), _journey_event(*_LOCATION_REFS), _event("event-003"))

    split_event(draft, "event-002", _THREE_PARTS, _journey_request())

    assert [event.client_event_id for event in draft.events] == [
        "event-001",
        "event-002-1",
        "event-002-2",
        "event-002-3",
        "event-003",
    ]


def test_split_event_never_drops_evidence():
    """호출자에게 근거를 맡기면 빠뜨린다. 코드가 나눠 담고 하나도 버리지 않는다."""

    refs = (*_LOCATION_REFS, (EventSourceType.PHOTO, PHOTO), (EventSourceType.CALENDAR, CALENDAR))
    draft = _draft(_journey_event(*refs))

    pieces = split_event(draft, "event-002", _THREE_PARTS, _journey_request())

    kept = {raw_id for piece in pieces for raw_id in _raw_ids(piece)}
    assert kept == {raw_id for _, raw_id in refs}


def test_split_event_puts_a_photo_in_exactly_one_part():
    refs = (*_LOCATION_REFS, (EventSourceType.PHOTO, PHOTO))
    draft = _draft(_journey_event(*refs))

    pieces = split_event(draft, "event-002", _THREE_PARTS, _journey_request())

    holders = [piece.title for piece in pieces if PHOTO in _raw_ids(piece)]
    assert holders == ["회사에서 근무"]  # 12:30 에 찍었다


def test_split_event_shares_a_long_stay_between_the_parts_it_covers():
    """긴 체류 하나를 오전·오후로 나누면 두 조각 다 그 체류가 근거다."""

    draft = _draft(_journey_event((EventSourceType.STAY, OFFICE)))
    draft.events[0].start_time = draft.events[0].start_time.replace(hour=9, minute=0)
    draft.events[0].end_time = draft.events[0].end_time.replace(hour=18)

    pieces = split_event(
        draft,
        "event-002",
        [
            _part("09:00", "12:00", "회사에서 오전 근무", eventType="WORK"),
            _part("13:00", "18:00", "회사에서 오후 근무", eventType="WORK"),
        ],
        _journey_request(),
    )

    assert [_raw_ids(piece) for piece in pieces] == [[OFFICE], [OFFICE]]


def test_split_event_keeps_a_movement_out_of_the_part_it_barely_touches():
    """경계에서 몇 분 걸친 이동까지 옆 조각에 담으면 확정이 그 조각의 시간을 다시 늘린다."""

    draft = _draft(_journey_event(*_LOCATION_REFS))

    pieces = split_event(
        draft,
        "event-002",
        [
            _part("08:20", "08:55", "회사로 출근"),
            _part("08:55", "18:05", "회사에서 근무", eventType="WORK"),
            _part("18:05", "19:00", "집으로 귀가"),
        ],
        _journey_request(),
    )

    assert _raw_ids(pieces[1]) == [OFFICE]


def test_split_event_clears_place_and_question_for_the_parts():
    """장소는 조각마다 다를 수 있다. 비워 두면 확정이 그 조각의 근거에서 다시 채운다."""

    draft = _draft(_journey_event(*_LOCATION_REFS))

    pieces = split_event(draft, "event-002", _THREE_PARTS, _journey_request())

    assert [piece.place for piece in pieces] == [None, None, None]
    assert [piece.address for piece in pieces] == [None, None, None]
    assert [piece.question for piece in pieces] == [None, None, None]
    # 주지 않은 필드는 원래 event 의 값을 이어받는다.
    assert pieces[0].tags == ["이동"]
    assert pieces[0].confidence == 0.8


def test_split_event_accepts_an_address_for_a_part():
    """주소는 받되 믿지 않는다. 근거에 없는 주소는 확정 pass 가 지운다.

    받지 않았을 때 실제 LLM 은 주소를 넣은 같은 호출을 되풀이해 반복 횟수를 다 썼다.
    """

    draft = _draft(_journey_event(*_LOCATION_REFS))
    parts = [dict(part) for part in _THREE_PARTS]
    parts[1]["address"] = "서울특별시 강남구 테헤란로 311"

    pieces = split_event(draft, "event-002", parts, _journey_request())

    assert pieces[1].address == "서울특별시 강남구 테헤란로 311"


def test_split_event_lets_a_part_name_its_place():
    draft = _draft(_journey_event(*_LOCATION_REFS))
    parts = [dict(part) for part in _THREE_PARTS]
    parts[1]["place"] = "회사"

    pieces = split_event(draft, "event-002", parts, _journey_request())

    assert pieces[1].place == "회사"


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ([_part("08:20", "19:00", "하나뿐")], "둘 이상"),
        (
            [_part("08:20", "09:00", "앞"), {"startTime": f"{DAY}T09:00:00+09:00", "title": "끝 없음"}],
            "필요한 필드",
        ),
        (
            [_part("08:20", "09:00", "앞"), _part("09:00", "18:00", "뒤", sourceRefs=[])],
            "쓸 수 없는 필드",
        ),
        (
            [_part("06:00", "07:00", "event 보다 앞"), _part("09:00", "18:00", "뒤")],
            "남는 조각이 둘보다 적습니다",
        ),
        (
            [_part("08:20", "09:00", "앞"), _part("18:00", "19:00", "뒤")],
            "어느 조각에도 담기지 않는 구간",
        ),
        (
            [_part("08:20", "12:00", "앞"), _part("11:00", "18:00", "겹침")],
            "겹칩니다",
        ),
        (
            [_part("08:20", "09:00", "앞"), _part("09:00", "08:00", "거꾸로")],
            "스키마 검증",
        ),
    ],
)
def test_split_event_rejects_bad_parts_without_touching_the_event(parts, message):
    draft = _draft(_journey_event(*_LOCATION_REFS))
    before = draft.model_copy(deep=True)

    with pytest.raises(DraftEditError, match=message):
        split_event(draft, "event-002", parts, _journey_request())

    assert draft == before


def test_split_event_keeps_only_the_inside_of_a_part_that_sticks_out():
    """밖으로 걸친 조각은 안쪽만 남긴다. 오류로 돌려주면 같은 호출이 되풀이된다."""

    draft = _draft(_journey_event(*_LOCATION_REFS))

    pieces = split_event(
        draft,
        "event-002",
        [
            _part("07:00", "09:00", "회사로 출근"),
            _part("09:00", "18:00", "회사에서 근무", eventType="WORK"),
            _part("18:00", "20:00", "집으로 귀가"),
        ],
        _journey_request(),
    )

    assert [(piece.start_time.hour, piece.start_time.minute) for piece in pieces] == [
        (8, 20),
        (9, 0),
        (18, 0),
    ]
    assert pieces[-1].end_time.hour == 19


def test_split_event_drops_a_part_that_lies_wholly_outside():
    """요청 시간 범위 끝에서 잘린 event 는 근거가 그 뒤까지 이어져 있다."""

    event = _journey_event(*_LOCATION_REFS)
    event.end_time = event.end_time.replace(hour=18, minute=0)
    draft = _draft(event)

    pieces = split_event(
        draft,
        "event-002",
        [
            _part("08:20", "09:00", "회사로 출근"),
            _part("09:00", "18:00", "회사에서 근무", eventType="WORK"),
            _part("18:00", "19:00", "집으로 귀가"),
        ],
        _journey_request(),
    )

    assert [piece.title for piece in pieces] == ["회사로 출근", "회사에서 근무"]
    # event 밖에 있는 근거는 버리지 않고 가장 가까운 조각이 가진다.
    assert _raw_ids(pieces[1]) == [OFFICE, BACK]


def test_split_event_keeps_a_photo_moment_at_the_edge():
    """처음부터 길이가 0 인 조각은 event 안에 있으면 그대로 둔다.

    길이가 있던 조각이 맞춘 뒤 0 이 됐다면 통째로 밖에 있던 것이라 뺀다. 둘을 가르지
    않으면 event 가 끝나는 시각에 찍은 사진의 조각까지 빠진다.
    """

    request = _journey_request().model_copy(
        update={"photos": [photo_item("1", taken=f"{DAY}T19:00:00")]}
    )
    draft = _draft(_journey_event(*_LOCATION_REFS, (EventSourceType.PHOTO, PHOTO)))

    pieces = split_event(
        draft,
        "event-002",
        [
            _part("08:20", "09:00", "회사로 출근"),
            _part("09:00", "18:00", "회사에서 근무", eventType="WORK"),
            _part("18:00", "19:00", "집으로 귀가"),
            _part("19:00", "19:00", "도착해서 남긴 사진", eventType="PHOTO_MOMENT"),
        ],
        request,
    )

    assert [piece.title for piece in pieces][-1] == "도착해서 남긴 사진"
    # 사진은 그 순간의 조각이 가진다. 그 시각에 끝나는 옆 조각이 아니다.
    assert [_raw_ids(piece) for piece in pieces][-2:] == [[BACK], [PHOTO]]


def test_split_event_names_the_evidence_no_part_covers():
    """조각이 덮지 않은 시간의 체류가 하루에서 조용히 사라지면 안 된다."""

    draft = _draft(_journey_event(*_LOCATION_REFS))

    with pytest.raises(DraftEditError, match="STAY 09:00:00~18:00:00"):
        split_event(
            draft,
            "event-002",
            [_part("08:20", "09:00", "출근"), _part("18:00", "19:00", "귀가")],
            _journey_request(),
        )


def test_split_event_rejects_a_part_without_evidence():
    """근거가 없는 시간으로 나누면 근거 없는 event 가 생긴다."""

    draft = _draft(_journey_event((EventSourceType.MOVEMENT, OUT), (EventSourceType.PHOTO, PHOTO)))
    before = draft.model_copy(deep=True)

    with pytest.raises(DraftEditError, match="근거가 없습니다"):
        split_event(
            draft,
            "event-002",
            [
                _part("08:20", "09:00", "출근"),
                _part("12:00", "13:00", "점심"),
                _part("15:00", "16:00", "근거 없는 시간"),
            ],
            _journey_request(),
        )

    assert draft == before


def test_split_event_reports_missing_event():
    draft = _draft(_event("event-001"))

    with pytest.raises(DraftEditError, match="event-999"):
        split_event(draft, "event-999", _THREE_PARTS, _journey_request())
