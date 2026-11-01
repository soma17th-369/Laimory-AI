"""이동 구간 하나는 이동 event 하나다 (#180).

뼈대가 하나로 묶은 이동을 Timeline·Repair 가 기록 경계로 다시 나눠도 코드가 맞추는지 본다. LLM 을
부르지 않는다 — Timeline 이 낼 만한 출력을 그대로 넣고 확정 pass 를 돌린다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import (
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    MovementItem,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.services.clue_bundles import build_timeline_clues
from app.services.draft_repair import repair_draft
from app.services.location_skeleton import SegmentKind, build_location_skeleton
from app.services.place_slots import fill_place_slots, keep_place_names_out
from app.services.skeleton_guard import NEEDS_WRITING, keep_one_event_per_journey
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    notification_item,
    stay_item,
)

_KST = timezone(timedelta(hours=9))
_LON = 127.0


def _at(hour, minute=0):
    return datetime(2026, 6, 20, hour, minute, tzinfo=_KST)


def _leg(label, start, end, from_lat, to_lat, *, transports=("IN_VEHICLE",)):
    return MovementItem(
        raw_id=fixture_raw_id(f"movement-{label}"),
        start_at=f"2026-06-20T{start}:00",
        end_at=f"2026-06-20T{end}:00",
        start=GeoPlace(latitude=from_lat, longitude=_LON, address=f"서울 출발{label}구 출발동 1"),
        end=GeoPlace(latitude=to_lat, longitude=_LON, address=f"서울 도착{label}구 도착동 1"),
        transports=list(transports),
    )


def _stay(label, start, end, lat):
    return stay_item(
        label,
        lat=lat,
        lon=_LON,
        start=f"2026-06-20T{start}:00",
        end=f"2026-06-20T{end}:00",
        places=[f"{label} 건물"],
        address=f"서울 {label}구 {label}동 5",
    )


def _journey_day():
    """집 → 기록 넷으로 갈린 여정(09~12시) → 사무실."""

    home = _stay("home", "07:00", "09:00", 37.40)
    legs = [
        _leg("a", "09:00", "10:00", 37.40, 37.45),
        _leg("b", "10:00", "11:00", 37.45, 37.50),
        _leg("c", "11:00", "11:30", 37.50, 37.55),
        _leg("d", "11:30", "12:00", 37.55, 37.60),
    ]
    office = _stay("office", "12:00", "18:00", 37.60)
    return make_request(stays=[home, office], movements=legs), legs


def _clues(request):
    return build_timeline_clues(build_location_skeleton(request), [], [], [])


def _event(records, start, end, *, event_type=EventType.MOVEMENT, title="이동", base=("E2",), extra=()):
    refs = [
        SourceRef(
            source_type=EventSourceType.MOVEMENT if isinstance(r, MovementItem) else EventSourceType.STAY,
            raw_id=r.raw_id,
        )
        for r in records
    ]
    return TimelineEventDraft(
        client_event_id=f"event-{title}",
        event_type=event_type,
        title=title,
        description="문장.",
        start_time=start,
        end_time=end,
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[*refs, *extra],
        base_event_ids=list(base),
    )


def _draft(*events):
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


def _movements(draft):
    return sorted(
        (e for e in draft.events if e.event_type is EventType.MOVEMENT), key=lambda e: e.start_time
    )


def _times(event):
    return event.start_time, event.end_time


def _raw_ids(event):
    return sorted(ref.raw_id for ref in event.source_refs)


def _everywhere(draft):
    return sorted(ref.raw_id for event in draft.events for ref in event.source_refs)


def test_fragments_of_one_journey_become_one_movement():
    request, legs = _journey_day()
    draft = _draft(
        _event(legs[:2], _at(9), _at(11), title="앞"),
        _event(legs[2:3], _at(11), _at(11, 30), title="가운데"),
        _event(legs[3:], _at(11, 30), _at(12), title="뒤"),
    )

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert _times(movement) == (_at(9), _at(12))
    assert _raw_ids(movement) == sorted(leg.raw_id for leg in legs)
    assert movement.merge_reason == NEEDS_WRITING, "조각의 글이 전체 여정을 말하지 않는다."
    assert movement.title == "앞", "코드는 글을 쓰지 않는다."


@pytest.mark.parametrize(
    ("index", "start", "end"),
    [(0, _at(9), _at(10)), (1, _at(10), _at(11)), (3, _at(11, 30), _at(12))],
    ids=["앞 조각", "가운데 조각", "뒤 조각"],
)
def test_an_event_that_used_part_of_a_journey_covers_all_of_it(index, start, end):
    """예전에는 복원이 빈 시간을 이동으로 되살려 09~10시와 09~12시가 겹쳐 남았다."""

    request, legs = _journey_day()
    draft = _draft(_event([legs[index]], start, end))

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert _times(movement) == (_at(9), _at(12))
    assert _raw_ids(movement) == sorted(leg.raw_id for leg in legs)
    assert movement.merge_reason == NEEDS_WRITING


def test_an_event_that_already_is_the_journey_is_left_alone():
    request, legs = _journey_day()
    draft = _draft(_event(legs, _at(9), _at(12), title="출근"))

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert movement.title == "출근"
    assert movement.merge_reason != NEEDS_WRITING


def test_a_journey_the_repair_wrote_is_not_handed_back():
    """확정은 Repair 반복마다 돈다. 다시 표시하면 Repair 가 쓴 글을 끝없이 되받는다."""

    request, legs = _journey_day()
    clues = _clues(request)
    draft = _draft(_event(legs[:2], _at(9), _at(11)), _event(legs[2:], _at(11), _at(12)))
    repair_draft(draft, request, extended=True, clues=clues)
    [movement] = _movements(draft)
    movement.merge_reason = "Repair 가 다시 썼다."

    repair_draft(draft, request, extended=True, clues=clues)

    [again] = _movements(draft)
    assert again.merge_reason == "Repair 가 다시 썼다."


@pytest.mark.parametrize("resumed", ["19:45", "20:23", "23:00"])
def test_a_journey_across_a_collection_gap_is_one_movement(resumed):
    """`a103f136`: 수집이 끊긴 기차 이동이 event 둘로 갈렸다."""

    first = _leg("a", "18:28", "19:16", 37.0, 37.5)
    second = _leg("b", resumed, "23:30", 38.4, 38.7)
    request = make_request(movements=[first, second])
    draft = _draft(
        _event([first], _at(18, 28), _at(19, 16), base=("E1",), title="앞"),
        _event([second], datetime.fromisoformat(f"2026-06-20T{resumed}:00+09:00"), _at(23, 30), base=("E1",), title="뒤"),
    )

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert _times(movement) == (_at(18, 28), _at(23, 30))
    assert _raw_ids(movement) == sorted([first.raw_id, second.raw_id])


def test_movements_apart_at_the_same_place_stay_two():
    """같은 곳에서 20분 이상 비면 머문 것이다. 합치지 않는다."""

    first = _leg("a", "09:00", "09:30", 37.40, 37.50)
    second = _leg("b", "11:00", "11:30", 37.50, 37.60)
    request = make_request(movements=[first, second])
    clues = _clues(request)
    assert len(clues.skeleton.segments) == 2
    draft = _draft(
        _event([first], _at(9), _at(9, 30), base=("E1",), title="앞"),
        _event([second], _at(11), _at(11, 30), base=("E2",), title="뒤"),
    )

    repair_draft(draft, request, extended=True, clues=clues)

    assert [_times(e) for e in _movements(draft)] == [(_at(9), _at(9, 30)), (_at(11), _at(11, 30))]


@pytest.mark.parametrize("stay_end", ["09:50", "10:00"], ids=["정확히 20분", "20분 초과"])
def test_a_movement_that_swallowed_a_stay_gives_it_back(stay_end):
    """20분 이상 체류는 앞뒤 이동을 나눈다. 코드가 나눠 Repair 의 판단에 맡기지 않는다."""

    first = _leg("a", "09:00", "09:30", 37.40, 37.50)
    station = _stay("station", "09:30", stay_end, 37.50)
    second = _leg("b", stay_end, "12:00", 37.50, 37.70)
    request = make_request(stays=[station], movements=[first, second])
    clues = _clues(request)
    assert [s.kind for s in clues.skeleton.segments] == [
        SegmentKind.MOVEMENT,
        SegmentKind.STAY,
        SegmentKind.MOVEMENT,
    ]
    draft = _draft(
        _event([first, station, second], _at(9), _at(12), base=("E1", "E2", "E3"), title="전체")
    )

    repair_draft(draft, request, extended=True, clues=clues)

    before, after = _movements(draft)
    assert _times(before) == (_at(9), _at(9, 30))
    assert after.end_time == _at(12)
    assert _raw_ids(before) == [first.raw_id] and _raw_ids(after) == [second.raw_id]
    [stay] = [e for e in draft.events if e.event_type is not EventType.MOVEMENT]
    assert stay.start_time == _at(9, 30) and stay.end_time == after.start_time
    assert _raw_ids(stay) == [station.raw_id]
    assert _everywhere(draft) == sorted([first.raw_id, station.raw_id, second.raw_id]), "rawId 는 한 곳에만 있다."
    assert after.title == "전체" and after.merge_reason == NEEDS_WRITING


def test_a_stay_inside_a_movement_event_goes_back_without_rewriting_the_movement():
    """체류 근거만 빠진 이동은 경로가 그대로라 글을 다시 맡기지 않는다."""

    home = _stay("home", "07:00", "09:00", 37.40)
    trip = _leg("a", "09:00", "10:00", 37.40, 37.60)
    request = make_request(stays=[home], movements=[trip])
    draft = _draft(_event([home, trip], _at(9), _at(10), base=("E2",)))

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert _raw_ids(movement) == [trip.raw_id]
    assert movement.merge_reason != NEEDS_WRITING
    assert home.raw_id in _everywhere(draft)


def test_a_transport_payment_event_is_the_journey_it_falls_on():
    """결제로 만든 이동은 위치 근거가 없어 시간으로 찾는다. 안 찾으면 복원이 이동을 하나 더 만든다."""

    request, legs = _journey_day()
    payment = notification_item("pay", "코레일", "승차권 결제", posted="2026-06-20T09:30:00")
    event = _event(
        [],
        _at(9, 30),
        _at(9, 40),
        base=(),
        extra=[SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=payment.raw_id)],
    )
    draft = _draft(event)

    keep_one_event_per_journey(draft, request, _clues(request))

    assert _times(event) == (_at(9), _at(12))
    assert set(_raw_ids(event)) == {payment.raw_id, *(leg.raw_id for leg in legs)}
    assert event.merge_reason == NEEDS_WRITING


def test_a_scheduled_movement_keeps_its_time():
    """일정 event 의 시간은 바꾸지 않는다(사용자 결정). 그 구간의 다른 이동 event 도 그대로 둔다."""

    request, legs = _journey_day()
    train = calendar_item("t", "기차", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00")
    scheduled = _event(
        legs[:1],
        _at(9),
        _at(10),
        title="기차",
        extra=[SourceRef(source_type=EventSourceType.CALENDAR, raw_id=train.raw_id)],
    )
    office = request.stays[1]
    rest = _event([*legs[1:], office], _at(10), _at(12), title="나머지")
    draft = _draft(scheduled, rest)

    keep_one_event_per_journey(draft, request, _clues(request))

    assert _times(scheduled) == (_at(9), _at(10))
    assert _times(rest) == (_at(10), _at(12))
    assert office.raw_id in _raw_ids(rest), "일정이 있는 구간의 event 는 근거도 건드리지 않는다."


def _train_day():
    """동대구역 체류 → 기차 안에서 끊김 → 세종에서 다시 잡힌 이동 → 서울 사무실. `d5b039b1` 의 아침이다."""

    station = stay_item(
        "station",
        lat=35.8797,
        lon=128.6286,
        start="2026-06-20T08:44:00",
        end="2026-06-20T09:14:00",
        places=["동대구역"],
        address="대구광역시 동구 동대구로 550",
    )
    train = MovementItem(
        raw_id=fixture_raw_id("movement-train"),
        start_at="2026-06-20T10:12:00",
        end_at="2026-06-20T12:03:00",
        start=GeoPlace(latitude=36.53, longitude=127.37, address="세종특별자치시 부강면 노호리 383-11"),
        end=GeoPlace(latitude=37.5446, longitude=126.9520, address="서울특별시 마포구 마포대로 89"),
        transports=["IN_VEHICLE"],
    )
    office = stay_item(
        "office",
        lat=37.5446,
        lon=126.9520,
        start="2026-06-20T12:03:00",
        end="2026-06-20T18:00:00",
        places=["포스트타워"],
        address="서울특별시 마포구 마포대로 89",
    )
    return make_request(stays=[station, office], movements=[train]), station, train


def test_a_train_after_the_station_stays_a_movement_from_the_station():
    request, station, train = _train_day()
    draft = _draft(_event([train], _at(10, 12), _at(12, 3), title="이동", base=("E2",)))

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert _times(movement) == (_at(9, 14), _at(12, 3)), "뒤 단계가 원본 시각으로 줄이지 않는다."
    assert _raw_ids(movement) == [train.raw_id]
    stay = next(e for e in draft.events if station.raw_id in _raw_ids(e))
    assert stay.event_type is not EventType.MOVEMENT and stay.end_time == _at(9, 14)


def test_a_train_restored_by_the_code_says_it_left_the_station():
    request, _, _ = _train_day()
    draft = _draft()

    repair_draft(draft, request, extended=True, clues=_clues(request))

    [movement] = _movements(draft)
    assert _times(movement) == (_at(9, 14), _at(12, 3))
    assert "동대구로" in movement.title and "부강면" not in movement.title


@pytest.mark.parametrize("fix", [fill_place_slots, keep_place_names_out])
def test_the_code_route_of_a_train_starts_at_the_station(fix):
    """경로를 기록의 입력에서 읽는 자리다. 그대로 두면 Repair 에 세종 출발로 고치라고 넘긴다."""

    request, _, train = _train_day()
    clues = _clues(request)
    event = _event([train], _at(9, 14), _at(12, 3), title="{place}에서 이동", base=("E2",))
    event.description = "{place}에서 이동했다."
    draft = _draft(event)

    fix(draft, request, clues)

    assert "동대구로" in event.title and "부강면" not in event.title
    assert "동대구로" in event.description and "부강면" not in event.description


def test_a_workout_holding_a_movement_record_is_not_a_journey():
    """헬스장 운동에 오가는 기록이 섞여도 운동이 이동 시간으로 줄지 않는다."""

    way = _leg("a", "18:40", "19:00", 37.40, 37.50)
    gym = _stay("gym", "19:00", "20:00", 37.50)
    request = make_request(stays=[gym], movements=[way])
    event = _event([way, gym], _at(18, 40), _at(20), event_type=EventType.EXERCISE, base=("E2",))
    draft = _draft(event)

    keep_one_event_per_journey(draft, request, _clues(request))

    assert _times(event) == (_at(18, 40), _at(20))
    assert _raw_ids(event) == sorted([way.raw_id, gym.raw_id])


def test_a_walk_written_in_pieces_is_one_walk():
    out = _leg("out", "19:00", "19:20", 37.40, 37.408, transports=("WALKING",))
    back = _leg("back", "19:20", "19:40", 37.408, 37.40, transports=("WALKING",))
    request = make_request(movements=[out, back])
    clues = _clues(request)
    assert [s.kind for s in clues.skeleton.segments] == [SegmentKind.WALK]
    draft = _draft(
        _event([out], _at(19), _at(19, 20), event_type=EventType.EXERCISE, base=("E1",), title="가는 길"),
        _event([back], _at(19, 20), _at(19, 40), event_type=EventType.EXERCISE, base=("E1",), title="오는 길"),
    )

    keep_one_event_per_journey(draft, request, clues)

    [walk] = draft.events
    assert _times(walk) == (_at(19), _at(19, 40))
    assert _raw_ids(walk) == sorted([out.raw_id, back.raw_id])
