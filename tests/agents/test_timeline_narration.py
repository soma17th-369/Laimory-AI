"""Timeline v4 는 코드가 정한 event 에 글만 쓴다 (#202).

모델 응답을 전체 병합·사진별 분할·방문 누락·사진/일정 id 중복·누락·교환·순서 변경으로 바꿔 넣어도, Repair 에
들어가기 전 draft 의 event 수·시간·위치 기록·사진·일정 rawId 는 코드의 기준과 같아야 한다. 실제 LLM 은 부르지
않는다.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.timeline.timeline_agent import TimelineAgent, parse_timeline_narration
from app.schemas import CalendarItem, EventSourceType, EventType, NotificationItem
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.base_events import NEEDS_WRITING, structural_refs
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import build_timeline_clues
from app.services.draft_repair import repair_draft
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import calendar_item, fixture_raw_id, make_request, photo_item, stay_item

_KST = timezone(timedelta(hours=9))
_STRUCTURAL = (EventSourceType.STAY, EventSourceType.MOVEMENT, EventSourceType.PHOTO, EventSourceType.CALENDAR)


def _at(hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 6, 20, hour, minute, tzinfo=_KST)


def _t(hhmm: str) -> str:
    return f"2026-06-20T{hhmm}:00"


def _photo_pair(label, hhmm, *, lat=None, lon=None, places=(), activity=None):
    item = photo_item(label, taken=_t(hhmm), lat=lat, lon=lon, places=list(places))
    clue = PhotoClue(
        clue_id=label, raw_id=item.raw_id, taken_at=_at(hhmm), latitude=lat, longitude=lon,
        places=list(places), activity=activity,
    )
    return item, clue


# --- 하루 두 가지 ------------------------------------------------------------------------------


def _photo_day():
    """백화점에서 장식 둘과 디저트, 공연장에서 시간 간격을 둔 실내·입구 사진(운영 사례를 합성 좌표로)."""

    stays = [
        stay_item("store-1", lat=37.5000, lon=127.0000, start=_t("10:47"), end=_t("11:52"), places=["OO백화점"], address="서울 중구 명동 1"),
        stay_item("store-2", lat=37.5002, lon=127.0001, start=_t("13:43"), end=_t("14:16"), places=["OO백화점"], address="서울 중구 명동 1"),
        stay_item("store-3", lat=37.5007, lon=126.9995, start=_t("14:33"), end=_t("15:24"), places=["OO백화점"], address="서울 중구 명동 1"),
        stay_item("hall-1", lat=37.5060, lon=127.0070, start=_t("15:32"), end=_t("16:00"), places=["XX음악감상실"], address="서울 중구 을지로 2"),
        stay_item("hall-2", lat=37.5061, lon=127.0081, start=_t("17:13"), end=_t("17:37"), places=["XX음악감상실"], address="서울 중구 을지로 2"),
        stay_item("hall-3", lat=37.5056, lon=127.0076, start=_t("17:44"), end=_t("19:08"), places=["XX음악감상실"], address="서울 중구 을지로 2"),
    ]
    pairs = [
        _photo_pair("P1", "14:39", lat=37.5001, lon=127.0000, activity="크리스마스 장식 앞에서 시간을 보냈다"),
        _photo_pair("P2", "14:41", lat=37.5000, lon=126.9999, activity="크리스마스 장식 앞에 다녀왔다"),
        _photo_pair("P3", "15:01", lat=37.5008, lon=126.9994, activity="망고 디저트를 먹었다"),
        _photo_pair("P4", "15:50", lat=37.5063, lon=127.0072, activity="공연장 같은 실내에서 시간을 보냈다"),
        _photo_pair("P5", "17:19", lat=37.5062, lon=127.0080, activity="음악감상실 입구에 다녀왔다"),
    ]
    request = make_request(stays=stays, photos=[item for item, _ in pairs])
    clues = build_timeline_clues(build_location_skeleton(request), [], [clue for _, clue in pairs], [])
    return request, clues


def _calendar_day():
    """사무실 방문 안의 회의와 점심 약속, 저녁 독립 일정, 종일 일정 둘, 하루 대부분을 덮는 일정."""

    office = stay_item("office", lat=37.50, start=_t("09:00"), end=_t("18:00"), places=["OO타워", "OO식당"], address="서울 마포구 도화동 1")
    calendars = [
        calendar_item("meeting", "주간 회의", start=_t("14:00"), end=_t("15:00")),
        calendar_item("lunch", "팀 점심", start=_t("12:00"), end=_t("13:00"), location_text="OO식당"),
        calendar_item("study", "온라인 스터디", start=_t("20:00"), end=_t("21:00")),
        calendar_item("prep", "출장 준비", start=_t("08:00"), end=_t("21:00")),
        CalendarItem(raw_id=fixture_raw_id("calendar-holiday"), start_at=_t("00:00"), end_at="2026-06-21T00:00:00", title="기념일", all_day=True),
        CalendarItem(raw_id=fixture_raw_id("calendar-trip"), start_at=_t("00:00"), end_at="2026-06-21T00:00:00", title="부산 출장", all_day=True),
    ]
    pairs = [
        _photo_pair("P1", "12:20", places=["OO식당"], activity="비빔밥을 먹었다"),
        _photo_pair("P2", "16:00", activity="화이트보드 앞에서 시간을 보냈다"),
        _photo_pair("P3", "20:30", activity="노트북 화면 앞에서 공부했다"),
    ]
    request = make_request(stays=[office], calendars=calendars, photos=[item for item, _ in pairs])
    clues = build_timeline_clues(
        build_location_skeleton(request), build_calendar_clues(request), [clue for _, clue in pairs], []
    )
    return request, clues


def _expected(clues):
    """코드의 기준: 기본 event 마다 (시간, 위치 기록, 사진, 일정)."""

    return sorted(
        (
            base.start,
            base.end,
            _ids(structural_refs(base), EventSourceType.STAY, EventSourceType.MOVEMENT),
            _ids(structural_refs(base), EventSourceType.PHOTO),
            _ids(structural_refs(base), EventSourceType.CALENDAR),
        )
        for base in clues.base_events
    )


def _actual(draft):
    return sorted(
        (
            event.start_time,
            event.end_time,
            _ids(event.source_refs, EventSourceType.STAY, EventSourceType.MOVEMENT),
            _ids(event.source_refs, EventSourceType.PHOTO),
            _ids(event.source_refs, EventSourceType.CALENDAR),
        )
        for event in draft.events
    )


def _ids(refs, *types):
    return tuple(sorted(ref.raw_id for ref in refs if ref.source_type in types))


def _narration(event_id, title, description="{place}에서 시간을 보냈다.", *, event_type="STAY", evidence=()):
    return {
        "id": event_id, "eventType": event_type, "title": title, "description": description,
        "evidence": list(evidence), "confidence": 0.7, "inferenceLevel": "EVIDENCE_BASED", "reason": "근거",
    }


def _ids_of(clues, kind):
    return [base.base_id for base in clues.base_events if base.kind.value == kind]


# --- 주입할 모델 응답 ----------------------------------------------------------------------------


def _photo_answers(clues):
    store, hall = _ids_of(clues, "STAY")
    good = [
        _narration(store, "망고 디저트를 먹음", "{place}의 크리스마스 장식 앞에서 시간을 보내고 망고 디저트를 먹었다.", event_type="MEAL", evidence=["P1", "P2", "P3"]),
        _narration(hall, "중구 을지로에 다녀옴", "{place}에 다녀왔다.", evidence=["P4", "P5"]),
    ]
    return {
        "모든 event 에 글": good,
        "전체 병합": [
            _narration(store, "하루를 보냄", "하루 종일 돌아다녔다.", evidence=["P1", "P2", "P3", "P4", "P5"]),
        ],
        "사진별 분할": [
            _narration(store, "크리스마스 장식", evidence=["P1"]),
            _narration(store, "크리스마스 장식 2", evidence=["P2"]),
            _narration(store, "망고 디저트를 먹음", event_type="MEAL", evidence=["P3"]),
            _narration(hall, "실내", evidence=["P4"]),
            _narration(hall, "입구", evidence=["P5"]),
            _narration(f"{store}-2", "꺼낸 디저트", event_type="MEAL", evidence=["P3"]),
        ],
        "방문 누락": [],
        "사진 id 중복·누락·교환": [
            _narration(store, "실내", evidence=["P4", "P5", "P5"]),
            _narration(hall, "장식", evidence=["P1", "P3", "P9"]),
        ],
        "순서 변경": list(reversed(good)),
    }


def _calendar_answers(clues):
    ids = {base.calendar.title if base.calendar and not base.segment else base.kind.value: base.base_id for base in clues.base_events}
    office = ids["STAY"]
    good = [
        _narration(ids["기념일"], "기념일 일정이 있었음", "기념일 일정이 있었다.", event_type="CALENDAR_EVENT"),
        _narration(ids["부산 출장"], "부산 출장 일정이 있었음", "부산 출장 일정이 있었다.", event_type="CALENDAR_EVENT"),
        _narration(office, "일함", "{place}에서 일했다.", event_type="WORK", evidence=["P2"]),
        _narration(ids["팀 점심"], "비빔밥을 먹음", "{place}에서 비빔밥을 먹었다.", event_type="MEAL", evidence=["P1", "C2"]),
        _narration(ids["주간 회의"], "주간 회의를 함", "{place}에서 주간 회의를 했다.", event_type="MEETING", evidence=["C1"]),
        _narration(ids["출장 준비"], "출장 준비를 함", "출장 준비 일정이 있었다.", event_type="WORK"),
        _narration(ids["온라인 스터디"], "온라인 스터디를 함", "온라인 스터디를 했다.", event_type="CLASS", evidence=["P3"]),
    ]
    return {
        "모든 event 에 글": good,
        "하위 일정 누락": [item for item in good if item["id"] not in (ids["주간 회의"], ids["팀 점심"])],
        "일정 중복": [*good, good[4], {**good[3], "title": "또 점심"}],
        "전체 병합": [_narration(office, "하루 종일 일함", event_type="WORK", evidence=["C1", "C2", "C3", "C4", "C5", "C6", "P1", "P2", "P3"])],
        "사진·일정 id 교환": [
            {**good[3], "evidence": ["P3", "C3"]},
            {**good[6], "evidence": ["P1", "C2"]},
            {**good[4], "evidence": ["C4", "P2"]},
        ],
        "순서 변경": list(reversed(good)),
    }


def _response(events):
    return json.dumps({"conversations": [], "events": events, "warnings": []}, ensure_ascii=False)


@pytest.mark.parametrize("answer", list(_photo_answers(_photo_day()[1])))
def test_any_answer_on_the_photo_day_keeps_the_codes_structure(answer):
    request, clues = _photo_day()
    text = _response(_photo_answers(clues)[answer])

    draft = parse_timeline_narration(text, request, clues)
    assert _actual(draft) == _expected(clues), "Timeline 결과"

    repair_draft(draft, request, extended=True, clues=clues)
    assert _actual(draft) == _expected(clues), "첫 확정 뒤(Repair 진입 전)"


@pytest.mark.parametrize("answer", list(_calendar_answers(_calendar_day()[1])))
def test_any_answer_on_the_calendar_day_keeps_the_codes_structure(answer):
    request, clues = _calendar_day()
    text = _response(_calendar_answers(clues)[answer])

    draft = parse_timeline_narration(text, request, clues)
    assert _actual(draft) == _expected(clues), "Timeline 결과"

    repair_draft(draft, request, extended=True, clues=clues)
    assert _actual(draft) == _expected(clues), "첫 확정 뒤(Repair 진입 전)"
    first, second = draft.events[:2]
    assert {first.title, second.title} <= {"기념일 일정이 있었음", "부산 출장 일정이 있었음"}, "종일 일정이 맨 앞이다."


def test_the_two_visits_hold_their_photos_once_each_through_the_confirm_pass():
    request, clues = _photo_day()
    draft = parse_timeline_narration(_response(_photo_answers(clues)["사진별 분할"]), request, clues)

    repair_draft(draft, request, extended=True, clues=clues)

    by_photo = {
        ref.raw_id: event.base_event_ids[0]
        for event in draft.events
        for ref in event.source_refs
        if ref.source_type is EventSourceType.PHOTO
    }
    store, hall = _ids_of(clues, "STAY")
    labels = {photo.raw_id: photo.clue_id for photo in clues.photos}
    assert {labels[raw_id]: base for raw_id, base in by_photo.items()} == {
        "P1": store, "P2": store, "P3": store, "P4": hall, "P5": hall,
    }
    assert len(draft.events) == 2


def test_the_calendar_day_is_built_by_the_rules():
    """방문과 그 안의 회의·점심은 따로다. 점심 사진은 장소가 맞는 점심이, 화이트보드는 방문이, 저녁 사진은 독립
    일정이 갖는다. 종일 일정은 맨 앞이고 출장 준비(13시간)는 시간순이다."""

    request, clues = _calendar_day()
    titles = [
        (base.calendar.title if base.calendar else base.kind.value, [p.clue_id for p in base.photos])
        for base in clues.base_events
    ]

    assert titles[:2] == [("기념일", []), ("부산 출장", [])] or titles[:2] == [("부산 출장", []), ("기념일", [])]
    assert titles[2:] == [
        ("출장 준비", []),
        ("STAY", ["P2"]),
        ("팀 점심", ["P1"]),
        ("주간 회의", []),
        ("온라인 스터디", ["P3"]),
    ]


# --- 글 얹기 ----------------------------------------------------------------------------------


def test_text_lands_on_the_event_and_notification_evidence_is_added():
    stay = stay_item("office", start=_t("09:00"), end=_t("18:00"))
    note = NotificationItem(raw_id=fixture_raw_id("notification-pay"), posted_at=_t("12:30"), app_name="토스", title="결제", text="본문")
    request = make_request(stays=[stay], notifications=[note])
    clue = NotificationClue(id="N1", kind=NotificationClueKind.PAYMENT, rawIds=[note.raw_id], firstAt=_at("12:30"), lastAt=_at("12:30"))
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [clue])
    text = _response([
        {**_narration("E1", "점심을 먹음", "{place}에서 점심을 먹었다.", event_type="MEAL", evidence=["N1", "E1"]),
         "startTime": _t("12:00") + "+09:00", "endTime": _t("13:00") + "+09:00"},
    ])

    [event] = parse_timeline_narration(text, request, clues).events

    assert (event.event_type, event.title, event.description) == (EventType.MEAL, "점심을 먹음", "{place}에서 점심을 먹었다.")
    assert (event.start_time, event.end_time) == (_at("09:00"), _at("18:00")), "시간은 코드가 정한 값이다."
    assert {ref.raw_id for ref in event.source_refs} == {stay.raw_id, note.raw_id}


@pytest.mark.parametrize("event_type", ["STAY", "SHOPPING", "CULTURE", "ERRAND"])
def test_v4_timeline_can_use_the_types_only_v4_has(event_type):
    request, clues = _photo_day()
    store, _ = _ids_of(clues, "STAY")

    draft = parse_timeline_narration(_response([_narration(store, "제목", event_type=event_type)]), request, clues)

    assert next(e for e in draft.events if e.base_event_ids == [store]).event_type.value == event_type


def test_unknown_ids_are_dropped_with_a_warning_and_the_first_duplicate_wins():
    request, clues = _photo_day()
    store, _ = _ids_of(clues, "STAY")
    text = _response([_narration(store, "첫째"), _narration(store, "둘째"), _narration("E99", "없는 event")])

    draft = parse_timeline_narration(text, request, clues)

    assert next(e for e in draft.events if e.base_event_ids == [store]).title == "첫째"
    assert any("입력에 없는 event id" in warning.message for warning in draft.warnings)


def test_movement_and_walk_keep_their_type_and_movement_is_not_given_to_a_stay():
    from app.schemas import GeoPlace, MovementItem

    home = stay_item("home", lat=37.50, start=_t("07:00"), end=_t("08:30"))
    work = stay_item("work", lat=37.60, start=_t("09:30"), end=_t("18:00"))
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at=_t("08:30"), end_at=_t("09:30"),
        start=GeoPlace(latitude=37.50, longitude=127.0), end=GeoPlace(latitude=37.60, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=12000,
    )
    request = make_request(stays=[home, work], movements=[commute])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    text = _response([
        _narration("E1", "이동", event_type="MOVEMENT"),
        _narration("E2", "출근함", "출근했다.", event_type="WORK"),
    ])

    events = parse_timeline_narration(text, request, clues).events

    assert [event.event_type for event in events] == [EventType.STAY, EventType.MOVEMENT, EventType.STAY]


def test_events_without_text_keep_the_frame_and_photo_or_calendar_ones_go_to_repair():
    request, clues = _calendar_day()

    draft = parse_timeline_narration(_response([]), request, clues)

    for event, base in zip(draft.events, clues.base_events):
        assert event.base_event_ids == [base.base_id]
        if base.needs_writing:
            assert event.merge_reason == NEEDS_WRITING
    lunch = next(e for e in draft.events if e.title.startswith("팀 점심"))
    assert lunch.description == "", "일정의 틀 문장은 일정의 내용을 묻어 비워 둔다."


def test_a_photo_event_without_text_keeps_what_the_photo_shows():
    item, clue = _photo_pair("P1", "22:56", activity="국물 요리와 만두를 먹었다")
    request = make_request(photos=[item])
    clues = build_timeline_clues(build_location_skeleton(request), [], [clue], [])

    [event] = parse_timeline_narration(_response([]), request, clues).events

    assert (event.title, event.description) == ("국물 요리와 만두를 먹었다", "국물 요리와 만두를 먹었다.")
    assert event.merge_reason == NEEDS_WRITING


# --- 글이 쓴 사진 대조 ---------------------------------------------------------------------------


def _citing(narration, *photo_ids):
    return {**narration, "photos": [{"id": photo_id, "activity": "한 일"} for photo_id in photo_ids]}


def _confirm_findings(draft, request, clues):
    from app.services.confirm_report import ConfirmReport

    report = ConfirmReport()
    repair_draft(draft, request, extended=True, clues=clues, report=report)
    return report.findings


def test_text_that_read_the_next_events_photos_is_not_taken():
    """같은 날 trace 재실행에서 본 모양이다. 입력은 맞았는데 Timeline 이 사진을 한 칸씩 밀려 읽어 백화점 디저트를
    공연장 글에 썼다. 그 글이 적은 사진 id 에 백화점의 P3 이 있어 코드가 알아본다."""

    request, clues = _photo_day()
    store, hall = _ids_of(clues, "STAY")
    text = _response([
        _citing(_narration(store, "크리스마스 장식 앞에 다녀옴", "{place}의 크리스마스 장식 앞에 다녀왔다."), "P1", "P2"),
        _citing(
            _narration(hall, "망고 디저트를 먹음", "{place}의 공연장에서 망고 디저트를 먹었다.", event_type="MEAL"),
            "P3", "P4", "P5",
        ),
    ])

    draft = parse_timeline_narration(text, request, clues)

    by_base = {event.base_event_ids[0]: event for event in draft.events}
    assert "디저트" not in by_base[hall].description, "다른 event 의 사진을 쓴 글은 받지 않는다."
    assert (by_base[hall].event_type, by_base[hall].merge_reason) == (EventType.STAY, NEEDS_WRITING)
    assert any("P3" in warning.message for warning in draft.warnings)
    assert by_base[store].title == "크리스마스 장식 앞에 다녀옴", "자기 사진만 적은 글은 받는다."
    assert by_base[store].photo_ids == ["P1", "P2"]
    assert _actual(draft) == _expected(clues), "어느 글을 받든 구조는 코드의 것이다."

    findings = _confirm_findings(draft, request, clues)
    missing = [f for f in findings if f["kind"] == "PHOTO_MISMATCH"]
    assert missing == [
        {
            "kind": "PHOTO_MISMATCH", "clientEventId": by_base[store].client_event_id, "title": by_base[store].title,
            "eventPhotos": ["P1", "P2", "P3"], "otherPhotos": [], "missingPhotos": ["P3"],
        }
    ], "빠진 사진은 짚어 Repair 에 넘긴다."
    assert any(f["kind"] == "EVENT_TO_WRITE" and f["clientEventId"] == by_base[hall].client_event_id for f in findings)


def test_text_that_names_every_own_photo_raises_nothing():
    request, clues = _photo_day()
    store, hall = _ids_of(clues, "STAY")
    text = _response([
        _citing(_narration(store, "망고 디저트를 먹음", "{place}의 장식 앞에 다녀온 뒤 망고 디저트를 먹었다."), "P1", "P2", "P3"),
        _citing(_narration(hall, "중구 을지로에 다녀옴", "{place}의 무대 앞에서 시간을 보내고 입구에 다녀왔다."), "P4", "P5"),
    ])

    draft = parse_timeline_narration(text, request, clues)

    assert not [f for f in _confirm_findings(draft, request, clues) if f["kind"] in ("PHOTO_MISMATCH", "EVENT_TO_WRITE")]


def test_a_photo_visit_written_with_the_frame_sentence_goes_to_repair():
    """사진 있는 방문에 틀 문장을 그대로 옮긴 글은 쓰지 않은 것이다(재실행 20칸 중 7칸)."""

    request, clues = _photo_day()
    store, hall = _ids_of(clues, "STAY")
    text = _response([
        _citing(_narration(store, "중구 명동에서 시간을 보냄"), "P1", "P2", "P3"),
        _citing(_narration(hall, "중구 을지로에 다녀옴", "{place}에 다녀왔다."), "P4", "P5"),
    ])

    by_base = {e.base_event_ids[0]: e for e in parse_timeline_narration(text, request, clues).events}

    assert by_base[store].merge_reason == NEEDS_WRITING
    assert by_base[hall].merge_reason != NEEDS_WRITING


def test_the_timeline_input_shows_photos_first_and_no_frame_sentence_for_photo_events():
    from app.services.clue_bundles import timeline_input_sections

    _, clues = _photo_day()

    for event in timeline_input_sections(clues)["events"]:
        assert list(event)[:5] == ["id", "kind", "startAt", "endAt", "photos"], "사진은 시간 바로 다음이다."
        assert "description" not in event["frame"], "사진 있는 event 에는 틀 문장을 보이지 않는다."


def test_the_narration_schema_asks_for_photos_before_the_text():
    from app.schemas import TimelineEventNarration

    properties = list(TimelineEventNarration.model_json_schema(by_alias=True)["properties"])

    assert properties.index("photos") < properties.index("title") < properties.index("description")


def test_a_failed_timeline_still_gives_the_days_events():
    """Timeline 이 실패하면 빈 draft 다. 확정 pass 가 코드의 event 를 틀 글로 채운다."""

    request, clues = _photo_day()
    draft = TimelineAgent(llm=FakeLLM(["깨진 응답"])).generate_from_clues(request, clues)

    assert draft.events == []
    repair_draft(draft, request, extended=True, clues=clues)
    assert _actual(draft) == _expected(clues)
