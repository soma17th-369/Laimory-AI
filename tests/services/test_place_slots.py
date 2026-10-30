"""장소 칸 검증 (#171, v4). 규칙은 사용자 결정(2026-10-29)과 App Server 공지다."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    MovementItem,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineResultEvent,
)
from app.schemas.clues import NotificationClue, NotificationClueKind
from app.services.clue_bundles import build_timeline_clues
from app.services.location_skeleton import build_location_skeleton
from app.services.place_slots import (
    confirm_place_slots,
    fill_place_slots,
    filled_description,
    place_names_in_title,
)
from app.services.timeline_result import build_result_request
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    notification_item,
    stay_item,
)

_KST = timezone(timedelta(hours=9))


def _event(source_refs, **overrides) -> TimelineEventDraft:
    values = dict(
        client_event_id="event-001",
        event_type=EventType.UNKNOWN,
        title="머문 시간",
        description="{place}에서 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 12, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 13, 0, tzinfo=_KST),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=source_refs,
    )
    values.update(overrides)
    return TimelineEventDraft(**values)


def _ref(item, source_type) -> SourceRef:
    return SourceRef(source_type=source_type, raw_id=item.raw_id)


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


def _clues(request, notifications=()):
    return build_timeline_clues(build_location_skeleton(request), [], [], list(notifications))


_LUNCH = stay_item(
    "lunch",
    start="2026-06-20T12:00:00",
    end="2026-06-20T13:00:00",
    places=["포스트타워 주차장", "OO식당", "OO정형외과"],
    address="서울특별시 마포구 마포대로 89",
)


def test_candidates_keep_only_names_from_the_pool_in_timeline_order():
    request = make_request(stays=[_LUNCH])
    event = _event(
        [_ref(_LUNCH, EventSourceType.STAY)],
        place_candidates=["OO정형외과", "지어낸 카페", "OO식당"],
    )
    draft = _draft(event)

    confirm_place_slots(draft, request, _clues(request))

    assert event.place_candidates == ["OO정형외과", "OO식당"]
    assert event.place == "OO정형외과", "App Server 처럼 1번 후보가 place 다."
    assert event.address == "서울특별시 마포구 마포대로 89"


def test_facilities_never_become_candidates():
    """주차장·대여소·정류장은 머문 곳이 될 수 없다. 실제 Timeline 이 주차장을 1번 후보로 골랐다."""

    request = make_request(stays=[_LUNCH])
    event = _event([_ref(_LUNCH, EventSourceType.STAY)], place_candidates=["포스트타워 주차장", "OO식당"])

    confirm_place_slots(_draft(event), request, _clues(request))

    assert "포스트타워 주차장" not in event.place_candidates
    assert event.place == "OO식당"


def test_stay_with_unknown_activity_keeps_the_slot_and_offers_the_names():
    """시간만 보냈다고 판단된 체류는 칸을 두고 후보를 준다. 주소는 이름이 하나도 없을 때뿐이다."""

    request = make_request(stays=[_LUNCH])
    event = _event([_ref(_LUNCH, EventSourceType.STAY)], place_candidates=[])

    confirm_place_slots(_draft(event), request, _clues(request))
    fill_place_slots(_draft(event), request)

    assert event.description == "{place}에서 시간을 보냈다."
    assert event.place_candidates == ["OO식당", "OO정형외과"]
    assert event.place == "OO식당"


def test_stay_without_any_name_falls_back_to_the_address():
    bare = stay_item("bare", start="2026-06-20T12:00:00", end="2026-06-20T13:00:00", places=[], address="전남 여수시 덕충동 2079")
    request = make_request(stays=[bare])
    event = _event([_ref(bare, EventSourceType.STAY)], place_candidates=[])

    confirm_place_slots(_draft(event), request, _clues(request))
    fill_place_slots(_draft(event), request)

    assert event.place_candidates == []
    assert event.description == "전남 여수시 덕충동 2079에서 시간을 보냈다.", "이름이 없으면 칸을 둘 수 없다(App Server 400)."


def test_same_place_shares_its_names_even_when_the_event_cites_a_record_without_names():
    """사무실 오후 기록·점심 사진처럼 이름 없는 근거만 댄 event 도 같은 장소(250m)의 이름을 후보로 받는다."""

    morning = stay_item(
        "morning", lat=37.5435, lon=126.9495, start="2026-06-20T09:00:00", end="2026-06-20T09:30:00",
        places=["SW마에스트로 연수센터"], address="서울 마포구 도화동 17-13",
    )
    afternoon = stay_item(
        "afternoon", lat=37.5436, lon=126.9496, start="2026-06-20T13:00:00", end="2026-06-20T22:00:00",
        places=[], address="서울 마포구 도화동 17-14",
    )
    request = make_request(stays=[morning, afternoon])
    event = _event(
        [_ref(afternoon, EventSourceType.STAY)],
        start_time=datetime(2026, 6, 20, 13, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 22, 0, tzinfo=_KST),
        place_candidates=[],
    )

    confirm_place_slots(_draft(event), request, _clues(request))
    fill_place_slots(_draft(event), request)

    assert event.place_candidates == ["SW마에스트로 연수센터"]
    assert event.description == "{place}에서 시간을 보냈다."


def test_candidates_are_capped_at_five():
    names = ["가", "나다", "라마", "바사", "아자", "차카"]
    stay = stay_item("many", places=names, start="2026-06-20T12:00:00", end="2026-06-20T13:00:00")
    request = make_request(stays=[stay])
    event = _event([_ref(stay, EventSourceType.STAY)], place_candidates=names)

    confirm_place_slots(_draft(event), request, _clues(request))

    assert len(event.place_candidates) == 5


def test_payment_merchant_is_the_first_candidate_not_a_confirmed_place():
    payment = notification_item("pay", "토스", "결제", posted="2026-06-20T12:30:00")
    clue = NotificationClue(
        clue_id="N1",
        kind=NotificationClueKind.PAYMENT,
        raw_ids=[payment.raw_id],
        counterpart="OO식당",
        summary="점심 결제",
    )
    request = make_request(stays=[_LUNCH], notifications=[payment])
    event = _event(
        [_ref(_LUNCH, EventSourceType.STAY), _ref(payment, EventSourceType.NOTIFICATION)],
        place_candidates=["포스트타워 주차장"],
    )

    confirm_place_slots(_draft(event), request, _clues(request, [clue]))

    assert event.place == "OO식당"
    assert event.place_candidates == ["OO식당", "OO정형외과"], "확실한 장소도 후보 1순위로 준다."


def test_calendar_place_in_the_pool_is_the_first_candidate():
    meeting = calendar_item("m", "회의", location_text="OO식당(서울특별시 마포구 마포대로 89)")
    request = make_request(stays=[_LUNCH], calendars=[meeting])
    event = _event([_ref(_LUNCH, EventSourceType.STAY), _ref(meeting, EventSourceType.CALENDAR)])

    confirm_place_slots(_draft(event), request, _clues(request))

    assert (event.place, event.place_candidates) == ("OO식당", ["OO식당", "OO정형외과"])


def test_calendar_only_event_uses_the_calendar_place():
    meeting = calendar_item("m", "회의", location_text="본사 3층 회의실")
    request = make_request(calendars=[meeting])
    event = _event([_ref(meeting, EventSourceType.CALENDAR)])

    confirm_place_slots(_draft(event), request, _clues(request))

    assert (event.place, event.place_candidates) == ("본사 3층 회의실", ["본사 3층 회의실"])


def test_every_event_with_names_gets_candidates_even_when_timeline_left_them_empty():
    """활동이 분명한 식사도 근거에 이름이 있으면 후보를 준다. 주소로 채우지 않는다."""

    request = make_request(stays=[_LUNCH])
    event = _event(
        [_ref(_LUNCH, EventSourceType.STAY)],
        event_type=EventType.MEAL,
        description="{place}에서 햄버거를 먹고 친구와 이야기했다.",
        place_candidates=[],
    )
    draft = _draft(event)

    confirm_place_slots(draft, request, _clues(request))
    fill_place_slots(draft, request)

    assert event.place_candidates == ["OO식당", "OO정형외과"]
    assert event.description == "{place}에서 햄버거를 먹고 친구와 이야기했다."


def test_slot_without_any_place_is_removed_with_its_particle():
    chat = notification_item("chat", "카카오톡", "김민수")
    request = make_request(notifications=[chat])
    event = _event([_ref(chat, EventSourceType.NOTIFICATION)], description="{place}에서 김민수와 이야기했다.")
    draft = _draft(event)

    confirm_place_slots(draft, request, None)
    fill_place_slots(draft, request)

    assert event.description == "김민수와 이야기했다."


def test_place_name_written_into_the_sentence_becomes_the_slot():
    request = make_request(stays=[_LUNCH])
    event = _event(
        [_ref(_LUNCH, EventSourceType.STAY)],
        description="OO식당에서 순두부찌개를 먹었다.",
        place_candidates=["OO식당"],
    )
    draft = _draft(event)

    confirm_place_slots(draft, request, _clues(request))
    fill_place_slots(draft, request)

    assert event.description == "{place}에서 순두부찌개를 먹었다."
    assert filled_description(event) == "OO식당에서 순두부찌개를 먹었다."


def test_input_address_written_into_the_sentence_becomes_the_slot():
    """후보가 있는데 Timeline 이 주소를 문장에 직접 쓴 실제 출력. 칸으로 돌려야 1번 후보가 보인다."""

    request = make_request(stays=[_LUNCH])
    event = _event(
        [_ref(_LUNCH, EventSourceType.STAY)],
        description="서울특별시 마포구 마포대로 89에서 순두부찌개를 먹었다.",
        place_candidates=["OO식당"],
    )

    confirm_place_slots(_draft(event), request, _clues(request))
    fill_place_slots(_draft(event), request)

    assert event.description == "{place}에서 순두부찌개를 먹었다."
    assert filled_description(event) == "OO식당에서 순두부찌개를 먹었다."


@pytest.mark.parametrize(
    ("counterpart", "written", "fixed"),
    [
        ("욱희민영", "욱희민영와 대화", "욱희민영과 대화"),
        ("💛예쁜하니💛", "예쁜하니과 대화", "예쁜하니와 대화"),
        ("💛울동생💛", "💛울동생💛와 이야기를 나눴다.", "💛울동생💛과 이야기를 나눴다."),
    ],
)
def test_particle_after_a_counterpart_name_follows_its_last_sound(counterpart, written, fixed):
    from app.services.place_slots import fix_counterpart_particles

    chat = notification_item("chat", "카카오톡", counterpart)
    request = make_request(notifications=[chat])
    clue = NotificationClue(
        clue_id="N1", kind=NotificationClueKind.CONVERSATION, counterpart=counterpart,
        summary="대화", raw_ids=[chat.raw_id],
    )
    event = _event([_ref(chat, EventSourceType.NOTIFICATION)], title=written, description=written)

    fix_counterpart_particles(_draft(event), request, _clues(request, [clue]))

    assert event.title == fixed
    assert event.description == fixed


def _movement(label, start, end, origin, destination):
    return MovementItem(
        raw_id=fixture_raw_id(f"movement-{label}"),
        start_at=start,
        end_at=end,
        start=GeoPlace(latitude=37.5, longitude=127.0, address=origin, places=["출발지 이름"]),
        end=GeoPlace(latitude=37.6, longitude=127.1, address=destination, places=["도착지 이름"]),
    )


def test_movement_sentence_uses_the_addresses_and_no_place_name():
    first = _movement("1", "2026-06-20T08:00:00", "2026-06-20T08:20:00", "서울 성동구 아차산로 100", "서울 중구 세종대로 110")
    last = _movement("2", "2026-06-20T08:25:00", "2026-06-20T09:00:00", "서울 중구 세종대로 110", "서울 강남구 강남대로 396")
    last.end.latitude = 37.9  # 경유지와 도착지는 다른 곳이다(같은 좌표면 같은 장소의 주소로 합쳐진다).
    request = make_request(movements=[first, last])
    event = _event(
        [_ref(last, EventSourceType.MOVEMENT), _ref(first, EventSourceType.MOVEMENT)],
        event_type=EventType.MOVEMENT,
        title="출근",
        description="",
        place_candidates=["도착지 이름"],
    )
    draft = _draft(event)

    confirm_place_slots(draft, request, None)
    fill_place_slots(draft, request)

    assert event.description == "성동구 아차산로에서 강남구 강남대로로 이동했다.", "주소 원문이 아니라 지명(구 + 동)이다."
    assert (event.place, event.place_candidates) == (None, [])
    assert event.address == "서울 강남구 강남대로 396"


@pytest.mark.parametrize(
    ("destination", "particle"),
    [
        ("서울 마포구 도화동 17-14", "마포구 도화동으로"),
        ("서울 강남구 강남대로 396", "강남구 강남대로로"),
        ("전남 여수시 덕충동 2079", "여수시 덕충동으로"),
        ("서울 관악구 지범로17길 5", "관악구 지범로17길로"),
    ],
)
def test_movement_direction_particle_follows_the_last_sound(destination, particle):
    movement = _movement("1", "2026-06-20T08:00:00", "2026-06-20T08:20:00", "서울 성동구 아차산로 100", destination)
    request = make_request(movements=[movement])
    event = _event([_ref(movement, EventSourceType.MOVEMENT)], event_type=EventType.MOVEMENT, description="")
    draft = _draft(event)

    fill_place_slots(draft, request)

    assert f"{particle} 이동했다." in event.description, "도착지는 지명이고 조사는 그 받침에 맞는다."


def test_movement_title_and_sentence_written_by_the_llm_are_kept():
    """이동 제목·문장은 Timeline·Repair 가 같은 지명으로 쓰고 코드는 덮어쓰지 않는다(사용자 결정 2026-10-30).

    제목은 Timeline, 문장은 코드가 따로 쓰자 기록되지 않은 열차 구간 때문에 둘의 출발지가 갈렸다.
    코드가 문장을 쓰는 것은 비었을 때뿐이다(위 두 테스트).
    """

    movement = _movement("1", "2026-06-20T08:00:00", "2026-06-20T08:40:00", "서울 성동구 아차산로 100", "서울 강남구 강남대로 396")
    request = make_request(movements=[movement])
    event = _event(
        [_ref(movement, EventSourceType.MOVEMENT)],
        event_type=EventType.MOVEMENT,
        title="성동구 아차산로에서 강남구 강남대로로 출근",
        description="성동구 아차산로에서 강남구 강남대로로 출근했다.",
    )

    fill_place_slots(_draft(event), request)

    assert event.title == "성동구 아차산로에서 강남구 강남대로로 출근"
    assert event.description == "성동구 아차산로에서 강남구 강남대로로 출근했다."


@pytest.mark.parametrize(
    "written",
    [
        # 기록되지 않은 구간 앞의 체류를 출발지로 썼다(실제 재실행 8번 중 1번).
        "중구 세종대로에서 성동구 아차산로로 출근",
        # 받침과 맞지 않는 조사(실제 재실행 `지범로17길으로`).
        "성동구 아차산로에서 강남구 강남대로으로 출근",
        # 장소명과 주소 원문은 이동에 쓰지 않는다(사용자 결정).
        "서울 성동구 아차산로 100에서 도착지 이름으로 출근",
        "출근",
    ],
)
def test_movement_route_follows_the_records_in_title_and_sentence_alike(written):
    """출발·도착 지명은 기록이 정하므로 코드가 맞추고, LLM 이 고른 목적은 둔다. 제목과 문장에 같은 규칙을
    써서 둘이 갈라지지 않는다(사용자 결정 2026-10-30 "title 이랑 description 이 안 맞으면 안되잖아")."""

    fixed = "성동구 아차산로에서 강남구 강남대로로 출근"

    movement = _movement("1", "2026-06-20T08:00:00", "2026-06-20T08:40:00", "서울 성동구 아차산로 100", "서울 강남구 강남대로 396")
    request = make_request(movements=[movement])
    event = _event(
        [_ref(movement, EventSourceType.MOVEMENT)],
        event_type=EventType.MOVEMENT,
        title=written,
        description=f"{written}했다.",
    )

    fill_place_slots(_draft(event), request)

    assert event.title == fixed
    assert event.description == f"{fixed}했다."


def test_place_name_in_title_is_found():
    request = make_request(stays=[_LUNCH])
    event = _event([_ref(_LUNCH, EventSourceType.STAY)], title="OO식당에서 점심", place_candidates=["OO식당"])

    assert place_names_in_title(event, request) == ["OO식당"]


# --- 결과 계약 (#171) ----------------------------------------------------------------


def test_result_sends_the_slot_and_the_candidates():
    request = make_request(stays=[_LUNCH])
    event = _event([_ref(_LUNCH, EventSourceType.STAY)], place_candidates=["OO식당", "OO정형외과"])
    draft = _draft(event)
    confirm_place_slots(draft, request, _clues(request))
    fill_place_slots(draft, request)

    body = build_result_request(draft, request).model_dump(by_alias=True, mode="json")

    [sent] = body["events"]
    assert sent["subtitle"] == "{place}에서 시간을 보냈다."
    assert sent["place"] == "OO식당"
    assert sent["placeCandidates"] == [{"name": "OO식당"}, {"name": "OO정형외과"}]


def test_contract_rejects_a_slot_without_place_or_candidates():
    with pytest.raises(ValidationError):
        TimelineResultEvent.model_validate(
            {
                "eventType": "UNKNOWN",
                "title": "머문 시간",
                "subtitle": "{place}에서 시간을 보냈다.",
                "startAt": "2026-06-20T12:00:00+09:00",
                "endAt": "2026-06-20T13:00:00+09:00",
                "sourceRawIds": [_LUNCH.raw_id],
            }
        )


def test_split_events_take_names_only_from_the_records_in_their_own_time():
    """같은 체류 구간을 나눈 두 event 가 서로의 기록 이름을 장소로 갖지 않는다(재실행에서 본 것)."""

    # 점심 자리와 사무실은 250m 넘게 떨어진 다른 장소다(실제 trace 는 384m).
    lunch = stay_item(
        "lunch-stay", lat=37.5470, lon=126.9450, start="2026-06-20T12:00:00", end="2026-06-20T12:33:00",
        places=["공덕파크자이 202동", "OO식당"], address="서울특별시 마포구 백범로 152",
    )
    long_stay = stay_item(
        "long-stay", lat=37.5436, lon=126.9496, start="2026-06-20T12:33:00", end="2026-06-20T22:00:00",
        places=[], address="서울 마포구 도화동 17-14",
    )
    request = make_request(stays=[lunch, long_stay])
    refs = [_ref(lunch, EventSourceType.STAY), _ref(long_stay, EventSourceType.STAY)]
    afternoon = _event(
        refs,
        start_time=datetime(2026, 6, 20, 12, 40, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 22, 0, tzinfo=_KST),
        place_candidates=["공덕파크자이 202동"],
    )
    draft = _draft(afternoon)

    confirm_place_slots(draft, request, _clues(request))
    fill_place_slots(draft, request)

    assert afternoon.place_candidates == []
    assert afternoon.address == "서울 마포구 도화동 17-14"
    assert afternoon.description == "서울 마포구 도화동 17-14에서 시간을 보냈다."
