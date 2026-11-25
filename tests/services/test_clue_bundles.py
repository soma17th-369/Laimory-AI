"""하루의 event 와 근거 귀속 (#169, #202).

코드가 Timeline 을 부르기 전에 event 마다 시간과 위치 기록·사진·일정 근거를 정한다. 규칙은
`clue_bundles` 모듈 docstring 과 #202 계획의 「귀속 규칙」에 있다.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import CalendarItem, EventSourceType, GeoPlace, MovementItem
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.base_events import BaseEventKind
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import (
    build_timeline_clues,
    expand_evidence,
    notification_evidence,
    timeline_input_sections,
)
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    notification_item,
    photo_item,
    stay_item,
)

_KST = timezone(timedelta(hours=9))


def _at(hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 6, 20, hour, minute, tzinfo=_KST)


def _t(hhmm: str) -> str:
    return f"2026-06-20T{hhmm}:00"


def _photo(label, hhmm, *, lat=None, lon=127.0, places=(), address=None, activity=None):
    return PhotoClue(
        clue_id=label,
        raw_id=fixture_raw_id(f"photo-{label}"),
        taken_at=_at(hhmm),
        latitude=lat,
        longitude=lon if lat is not None else None,
        places=list(places),
        address=address,
        activity=activity,
    )


def _conversation(label, first, last, kind=NotificationClueKind.CONVERSATION):
    return NotificationClue(
        clue_id=label,
        kind=kind,
        raw_ids=[fixture_raw_id(f"notification-{label}")],
        first_at=_at(first),
        last_at=_at(last),
        summary="요약",
    )


def _clues(stays=(), calendars=(), photos=(), notifications=(), movements=()):
    request = make_request(stays=list(stays), calendars=list(calendars), movements=list(movements))
    return build_timeline_clues(
        build_location_skeleton(request),
        build_calendar_clues(request),
        list(photos),
        list(notifications),
    )


def _home_work():
    home = stay_item("home", lat=37.50, start=_t("07:00"), end=_t("08:30"), places=["집 근처 아파트"])
    work = stay_item("work", lat=37.60, start=_t("09:30"), end=_t("18:00"), places=["회사 빌딩"])
    return home, work


def _base_of(clues, raw_id):
    """그 위치 기록의 구간 기본 event. 방문 안 일정도 그 기록을 장소 근거로 갖지만 구간의 event 는 하나다."""

    return next(base for base in clues.base_events if base.segment and raw_id in base.segment.raw_ids)


def _photo_ids(base):
    return [photo.clue_id for photo in base.photos]


# --- 두 방문과 그 사진 (#202) -------------------------------------------------------------------


def _two_visits(photo_order=None):
    """백화점에서 장식 사진 둘과 디저트, 900m 떨어진 공연장에서 시간 간격을 둔 실내·입구 사진.

    운영 사례(같은 날 두 실행)를 합성 좌표로 옮겼다. 두 방문은 이동 기록 없이 8분 공백으로 이어지고 각각
    30분 넘게 머물러 따로 방문이다(#201).
    """

    store = [
        stay_item("store-1", lat=37.5000, lon=127.0000, start=_t("10:47"), end=_t("11:52"), places=["OO백화점"], address="서울 중구 명동 1"),
        stay_item("store-2", lat=37.5002, lon=127.0001, start=_t("13:43"), end=_t("14:16"), places=["OO백화점"], address="서울 중구 명동 1"),
        stay_item("store-3", lat=37.5007, lon=126.9995, start=_t("14:33"), end=_t("15:24"), places=["OO백화점"], address="서울 중구 명동 1"),
    ]
    hall = [
        stay_item("hall-1", lat=37.5060, lon=127.0070, start=_t("15:32"), end=_t("16:00"), places=["XX음악감상실"], address="서울 중구 을지로 2"),
        stay_item("hall-2", lat=37.5061, lon=127.0081, start=_t("17:13"), end=_t("17:37"), places=["XX음악감상실"], address="서울 중구 을지로 2"),
        stay_item("hall-3", lat=37.5056, lon=127.0076, start=_t("17:44"), end=_t("19:08"), places=["XX음악감상실"], address="서울 중구 을지로 2"),
    ]
    photos = [
        _photo("P1", "14:39", lat=37.5001, lon=127.0000, activity="크리스마스 장식 앞에서 시간을 보냈다"),
        _photo("P2", "14:41", lat=37.5000, lon=126.9999, activity="크리스마스 장식 앞에 다녀왔다"),
        _photo("P3", "15:01", lat=37.5008, lon=126.9994, activity="망고 디저트를 먹었다"),
        _photo("P4", "15:50", lat=37.5063, lon=127.0072, activity="공연장 같은 실내에서 시간을 보냈다"),
        _photo("P5", "17:19", lat=37.5062, lon=127.0080, activity="음악감상실 입구에 다녀왔다"),
    ]
    if photo_order is not None:
        photos = [photos[i] for i in photo_order]
    return store, hall, photos


def test_two_visits_keep_their_own_photos_once_each():
    """첫 방문에 장식 둘과 디저트, 두 번째 방문에 실내·입구. 사진 event 를 따로 만들지 않는다(#202)."""

    store, hall, photos = _two_visits()

    clues = _clues(stays=[*store, *hall], photos=photos)

    visits = [base for base in clues.base_events if base.kind is BaseEventKind.STAY]
    assert [_photo_ids(base) for base in visits] == [["P1", "P2", "P3"], ["P4", "P5"]]
    assert not [base for base in clues.base_events if base.kind is BaseEventKind.PHOTO]
    first, second = visits
    assert (first.start, first.end) == (_at("10:47"), _at("15:24"))
    assert (second.start, second.end) == (_at("15:32"), _at("19:08"))
    every = [photo.raw_id for base in clues.base_events for photo in base.photos]
    assert sorted(every) == sorted(photo.raw_id for photo in photos), "사진은 정확히 한 번씩이다."


def test_the_same_input_in_any_order_gives_the_same_events():
    """같은 입력이면 같은 결과다. 입력 순서는 보지 않는다."""

    store, hall, photos = _two_visits()
    first = _clues(stays=[*store, *hall], photos=photos)
    rng = random.Random(202)
    for _ in range(5):
        stays = [*store, *hall]
        rng.shuffle(stays)
        shuffled = list(photos)
        rng.shuffle(shuffled)
        again = _clues(stays=stays, photos=shuffled)
        assert _signature(again) == _signature(first)


def _signature(clues):
    from app.services.base_events import structural_refs

    return [
        (base.base_id, base.kind, base.start, base.end, sorted(r.raw_id for r in structural_refs(base)))
        for base in clues.base_events
    ]


# --- 사진은 한 곳에만 ------------------------------------------------------------------------


def test_photo_goes_to_the_visit_that_holds_its_time():
    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "12:00")])

    assert _photo_ids(_base_of(clues, work.raw_id)) == ["P1"], "좌표가 없으면 시각이 방문 안일 때 그 방문이다."


def test_photo_at_a_boundary_goes_to_one_visit_at_its_spot():
    """경계 근처 사진은 위치가 맞는 방문 하나에만 든다. 예전에는 두 구간에 함께 붙었다."""

    home, work = _home_work()
    leaving = _photo("P1", "08:40", lat=37.5001)

    clues = _clues(stays=[home, work], photos=[leaving])

    holders = [base for base in clues.base_events if base.photos]
    assert [(base.base_id, _photo_ids(base)) for base in holders] == [(_base_of(clues, home.raw_id).base_id, ["P1"])]


def test_photo_twenty_minutes_before_a_visit_at_its_spot_belongs_to_it():
    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "09:15", lat=37.6001)])

    assert _photo_ids(_base_of(clues, work.raw_id)) == ["P1"]


def test_photo_far_from_every_visit_in_time_is_its_own_event_not_the_nearest():
    """가장 가까운 구간에 붙이지 않는다(#163). 20분 넘게 떨어진 빈 시간의 사진은 사진 event 다."""

    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "09:00", lat=37.6001), _photo("P2", "22:56", lat=37.9)])

    photo_events = [base for base in clues.base_events if base.kind is BaseEventKind.PHOTO]
    assert [_photo_ids(base) for base in photo_events] == [["P1"], ["P2"]]


def test_photo_inside_a_visit_but_far_from_it_is_its_own_event():
    """방문 시간 안이어도 250m 넘게 떨어진 곳의 사진은 그 방문의 근거가 아니다."""

    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "12:00", lat=37.70)])

    assert not _base_of(clues, work.raw_id).photos
    assert [base.kind for base in clues.base_events if base.photos] == [BaseEventKind.PHOTO]


def test_photos_of_one_scene_outside_the_visits_share_one_event():
    """2초 간격 저녁 사진 두 장이 식사 event 둘이 됐다(사용자 결정: 같은 장면 사진은 event 하나)."""

    clues = _clues(photos=[_photo("P1", "22:56"), _photo("P2", "22:57")])

    [scene] = clues.base_events
    assert _photo_ids(scene) == ["P1", "P2"]
    assert (scene.start, scene.end) == (_at("22:56"), _at("22:57"))


def test_photos_far_apart_in_time_are_two_events():
    clues = _clues(photos=[_photo("P1", "12:00"), _photo("P2", "13:00")])

    assert [_photo_ids(base) for base in clues.base_events] == [["P1"], ["P2"]]


def test_a_photo_during_a_movement_is_its_own_event():
    home = stay_item("home", lat=37.50, start=_t("07:00"), end=_t("08:30"), places=["집"])
    work = stay_item("work", lat=37.60, start=_t("09:30"), end=_t("18:00"), places=["회사"])
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at=_t("08:30"), end_at=_t("09:30"),
        start=GeoPlace(latitude=37.50, longitude=127.0), end=GeoPlace(latitude=37.60, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=12000,
    )

    clues = _clues(stays=[home, work], movements=[commute], photos=[_photo("P1", "09:00", lat=37.55)])

    assert [base.kind for base in clues.base_events] == [
        BaseEventKind.STAY, BaseEventKind.MOVEMENT, BaseEventKind.PHOTO, BaseEventKind.STAY,
    ]


# --- 일정 (#202) ---------------------------------------------------------------------------


def test_a_calendar_covering_a_visit_is_that_visit_and_keeps_the_visit_time():
    """방문을 덮는 일정은 그 방문에 연결한다. 방문의 경계는 위치 기록을 따른다."""

    office = stay_item("office", start=_t("13:05"), end=_t("16:50"), places=["OO타워"])
    meeting = calendar_item("m", "워크숍", start=_t("13:00"), end=_t("17:00"))

    clues = _clues(stays=[office], calendars=[meeting])

    [visit] = clues.base_events
    assert visit.calendar is not None and visit.calendar.title == "워크숍"
    assert (visit.start, visit.end) == (_at("13:05"), _at("16:50"))


def test_a_meeting_inside_a_long_visit_is_its_own_event():
    """09~18시 사무실 방문 안의 14~15시 회의는 방문과 회의 두 건이다."""

    office = stay_item("office", start=_t("09:00"), end=_t("18:00"), places=["OO타워"])
    meeting = calendar_item("m", "회의", start=_t("14:00"), end=_t("15:00"))

    clues = _clues(stays=[office], calendars=[meeting])

    visit, inner = clues.base_events
    assert visit.calendar is None and (visit.start, visit.end) == (_at("09:00"), _at("18:00"))
    assert inner.kind is BaseEventKind.CALENDAR and (inner.start, inner.end) == (_at("14:00"), _at("15:00"))
    assert inner.inside == visit.base_id
    assert [r.raw_id for r in inner.place_records] == [office.raw_id]


def test_a_calendar_spanning_two_visits_partly_is_its_own_event():
    home, work = _home_work()
    meeting = calendar_item("m", "아침 회의", start=_t("08:00"), end=_t("10:00"))

    clues = _clues(stays=[home, work], calendars=[meeting])

    assert all(base.calendar is None for base in clues.base_events if base.segment is not None)
    [own] = [base for base in clues.base_events if base.kind is BaseEventKind.CALENDAR]
    assert (own.start, own.end) == (_at("08:00"), _at("10:00"))
    assert sorted(r.raw_id for r in own.place_records) == sorted([home.raw_id, work.raw_id])


def test_a_calendar_with_no_visit_is_its_own_event():
    meeting = calendar_item("m", "온라인 회의", start=_t("21:00"), end=_t("22:00"))

    clues = _clues(calendars=[meeting])

    [own] = clues.base_events
    assert own.kind is BaseEventKind.CALENDAR and own.inside is None


def test_a_calendar_planned_in_another_district_does_not_take_the_visit():
    """일정의 예정 장소만으로 실제 방문 기록을 덮어쓰지 않는다."""

    office = stay_item("office", start=_t("13:05"), end=_t("16:50"), places=["OO타워"], address="서울 마포구 도화동 1")
    meeting = calendar_item(
        "m", "워크숍", start=_t("13:00"), end=_t("17:00"), location_text="강남 세미나실(서울 강남구 역삼동 737)"
    )

    clues = _clues(stays=[office], calendars=[meeting])

    visit = _base_of(clues, office.raw_id)
    [own] = [base for base in clues.base_events if base.kind is BaseEventKind.CALENDAR]
    assert visit.calendar is None
    assert own.inside is None and not own.place_records


def test_one_calendar_over_two_visits_takes_the_one_at_its_place():
    first = stay_item("cafe", lat=37.50, start=_t("12:05"), end=_t("13:00"), places=["XX카페"])
    second = stay_item("restaurant", lat=37.52, start=_t("13:10"), end=_t("13:55"), places=["OO식당"])
    lunch = calendar_item("l", "점심 약속", start=_t("12:00"), end=_t("14:00"), location_text="OO식당")

    clues = _clues(stays=[first, second], calendars=[lunch])

    assert _base_of(clues, second.raw_id).calendar is not None, "장소가 맞는 방문이 먼저다."
    assert _base_of(clues, first.raw_id).calendar is None
    assert not [base for base in clues.base_events if base.kind is BaseEventKind.CALENDAR]


def test_one_calendar_over_two_visits_without_a_place_takes_the_longer_overlap():
    first = stay_item("a", lat=37.50, start=_t("12:05"), end=_t("12:40"), places=["가"])
    second = stay_item("b", lat=37.52, start=_t("12:50"), end=_t("13:55"), places=["나"])
    lunch = calendar_item("l", "점심 약속", start=_t("12:00"), end=_t("14:00"))

    clues = _clues(stays=[first, second], calendars=[lunch])

    assert _base_of(clues, second.raw_id).calendar is not None
    assert _base_of(clues, first.raw_id).calendar is None


def test_two_calendars_over_one_visit_link_one_and_keep_the_other():
    office = stay_item("office", start=_t("14:05"), end=_t("14:55"), places=["OO타워"])
    meeting = calendar_item("m", "팀 회의", start=_t("14:00"), end=_t("15:00"))
    prep = calendar_item("p", "회의 준비", start=_t("14:00"), end=_t("15:00"))

    clues = _clues(stays=[office], calendars=[meeting, prep])

    visit = _base_of(clues, office.raw_id)
    [own] = [base for base in clues.base_events if base.kind is BaseEventKind.CALENDAR]
    assert {visit.calendar.title, own.calendar.title} == {"팀 회의", "회의 준비"}
    assert own.inside == visit.base_id


def test_a_long_timed_calendar_is_background_and_not_shown_first():
    """시작·종료 시각이 있는 장시간 일정은 길이만으로 하루 맨 앞에 두지 않는다."""

    home, work = _home_work()
    trip = calendar_item("trip", "출장", start=_t("08:00"), end=_t("21:00"))

    clues = _clues(stays=[home, work], calendars=[trip])

    kinds = [base.kind for base in clues.base_events]
    assert kinds == [BaseEventKind.STAY, BaseEventKind.CALENDAR, BaseEventKind.STAY]
    assert all(b.background_calendar_ids == ["C1"] for b in clues.bundles if b.segment)
    assert all(base.calendar is None for base in clues.base_events if base.segment)


def test_all_day_calendars_come_first_once_each_and_leave_the_visits_alone():
    """캘린더 원본의 종일 일정은 하루 맨 앞에 한 번씩이다. 방문의 경계와 사진은 그대로다."""

    home, work = _home_work()
    trip = CalendarItem(raw_id=fixture_raw_id("calendar-trip"), start_at=_t("00:00"), end_at="2026-06-21T00:00:00", title="부산 출장", all_day=True)
    holiday = CalendarItem(raw_id=fixture_raw_id("calendar-holiday"), start_at=_t("00:00"), end_at="2026-06-21T00:00:00", title="기념일", all_day=True)

    clues = _clues(stays=[home, work], calendars=[trip, holiday], photos=[_photo("P1", "12:00")])

    first, second, *rest = clues.base_events
    assert first.all_day and second.all_day
    assert {first.calendar.title, second.calendar.title} == {"부산 출장", "기념일"}
    assert [base.kind for base in rest] == [BaseEventKind.STAY, BaseEventKind.STAY]
    assert _photo_ids(_base_of(clues, work.raw_id)) == ["P1"], "배경 일정은 사진을 갖지 않는다."
    assert (rest[1].start, rest[1].end) == (_at("09:30"), _at("18:00"))


def test_an_untimed_calendar_makes_no_event():
    untimed = CalendarItem(raw_id=fixture_raw_id("calendar-untimed"), start_at="언젠가", end_at=None, title="미정")

    clues = _clues(calendars=[untimed])

    assert clues.base_events == []


# --- 방문과 일정이 겹칠 때 사진 (#202) ----------------------------------------------------------


def test_a_meeting_without_a_place_does_not_take_the_visit_photo():
    """시간만 겹친 일정이 방문 사진을 가져가지 않는다."""

    office = stay_item("office", start=_t("09:00"), end=_t("18:00"), places=["OO타워"])
    meeting = calendar_item("m", "회의", start=_t("14:00"), end=_t("15:00"))

    clues = _clues(stays=[office], calendars=[meeting], photos=[_photo("P1", "14:30")])

    visit, inner = clues.base_events
    assert _photo_ids(visit) == ["P1"] and inner.photos == ()


def test_a_meeting_at_the_photo_place_takes_the_photo():
    office = stay_item("office", start=_t("09:00"), end=_t("18:00"), places=["OO타워", "OO식당"])
    lunch = calendar_item("l", "점심", start=_t("12:00"), end=_t("13:00"), location_text="OO식당")

    clues = _clues(stays=[office], calendars=[lunch], photos=[_photo("P1", "12:20", places=["OO식당"])])

    visit, inner = clues.base_events
    assert visit.photos == () and _photo_ids(inner) == ["P1"]


def test_without_location_a_photo_goes_to_the_calendar_of_its_time():
    lunch = calendar_item("l", "점심 약속", start=_t("12:00"), end=_t("13:00"))

    clues = _clues(calendars=[lunch], photos=[_photo("P1", "12:20"), _photo("P2", "20:00")])

    calendar_event, photo_event = clues.base_events
    assert calendar_event.kind is BaseEventKind.CALENDAR and _photo_ids(calendar_event) == ["P1"]
    assert photo_event.kind is BaseEventKind.PHOTO and _photo_ids(photo_event) == ["P2"]


def test_a_background_calendar_never_takes_a_photo():
    trip = calendar_item("trip", "제주 여행", start=_t("06:00"), end=_t("23:00"))

    clues = _clues(calendars=[trip], photos=[_photo("P1", "12:00")])

    background, photo_event = clues.base_events
    assert background.photos == () and _photo_ids(photo_event) == ["P1"]


# --- 대화와 알림 ------------------------------------------------------------------------------


def test_a_conversation_is_always_its_own_bundle():
    """구간에 붙여 두자 Timeline 이 대화로 체류를 채웠다(#173). 대화는 위치와 따로다."""

    home, work = _home_work()

    clues = _clues(stays=[home, work], notifications=[_conversation("N1", "08:00", "10:00")])

    [holder] = [b for b in clues.bundles if b.conversations]
    assert holder.segment is None and holder.base_event is None
    assert (holder.start, holder.end) == (_at("08:00"), _at("10:00"))


def test_other_notifications_attach_to_the_one_segment_of_their_time():
    """시각이 없던 때 결제·소식은 근거로 거의 쓰이지 않았다(#173). 구간 하나에만 붙는다."""

    home, work = _home_work()
    payment = _conversation("N2", "12:30", "12:30", kind=NotificationClueKind.PAYMENT)
    news = _conversation("N3", "13:00", "18:30", kind=NotificationClueKind.NEWS)

    clues = _clues(stays=[home, work], notifications=[payment, news])

    [holder] = [b for b in clues.bundles if b.notifications]
    assert [c.clue_id for c in holder.notifications] == ["N2", "N3"]
    assert holder.segment.raw_ids == [work.raw_id]
    assert clues.day_notifications == []


def test_a_notification_outside_every_segment_or_for_another_day_stays_on_the_day_list():
    away = _conversation("N1", "20:00", "20:00", kind=NotificationClueKind.PAYMENT)
    later = _conversation("N2", "12:30", "12:30", kind=NotificationClueKind.RESERVATION)
    later.schedule_today = False

    clues = _clues(stays=_home_work(), notifications=[away, later])

    assert not any(b.notifications for b in clues.bundles)
    assert [c.clue_id for c in clues.day_notifications] == ["N1", "N2"]


def test_notification_evidence_takes_only_notifications_and_reports_the_rest():
    """모델이 고르는 근거는 알림·대화뿐이다(#202). 사진·일정·event id 는 코드의 근거라 펼치지 않는다."""

    home, work = _home_work()
    request = make_request(stays=[home, work], notifications=[notification_item("pay", "토스", "결제")])
    payment = _conversation("N2", "12:30", "12:30", kind=NotificationClueKind.PAYMENT)
    payment.raw_ids = [request.notifications[0].raw_id]
    clues = build_timeline_clues(build_location_skeleton(request), [], [_photo("P1", "12:00")], [payment])

    refs, ignored = notification_evidence(clues, ["N2", "P1", "E2", "N9"], request)

    assert [(ref.source_type, ref.raw_id) for ref in refs] == [(EventSourceType.NOTIFICATION, payment.raw_ids[0])]
    assert ignored == ["P1", "E2", "N9"]


def test_expand_evidence_reads_event_and_clue_ids_and_drops_unknown_ones():
    home, work = _home_work()
    request = make_request(stays=[home, work], photos=[photo_item("P1", taken=_t("12:00"), raw_id="photo-P1")])
    clues = build_timeline_clues(build_location_skeleton(request), [], [_photo("P1", "12:00")], [])
    work_id = _base_of(clues, work.raw_id).base_id

    refs = expand_evidence(clues, [work_id, "P1", "P1", "E9", "B1"], request)

    assert [r.raw_id for r in refs] == [work.raw_id, fixture_raw_id("photo-P1")]


# --- 프롬프트 입력 ---------------------------------------------------------------------------


def test_no_location_day_still_lists_photos_and_calendars_as_events():
    meeting = calendar_item("m", "회의", start=_t("10:00"), end=_t("11:00"))

    clues = _clues(calendars=[meeting], photos=[_photo("P1", "12:00")])

    assert not clues.skeleton.has_location
    assert [base.base_id for base in clues.base_events] == ["E1", "E2"]
    sections = timeline_input_sections(clues)
    assert sections["day"] == {"hasLocation": False}
    assert [entry["kind"] for entry in sections["events"]] == ["CALENDAR", "PHOTO"]


def test_event_ids_follow_display_order():
    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "23:00")])

    assert [base.base_id for base in clues.base_events] == ["E1", "E2", "E3"]
    assert _photo_ids(clues.base_events[-1]) == ["P1"]


def test_the_event_list_says_what_is_inside_what():
    office = stay_item("office", start=_t("09:00"), end=_t("18:00"), places=["OO타워"])
    meeting = calendar_item("m", "회의", start=_t("14:00"), end=_t("15:00"))

    events = timeline_input_sections(_clues(stays=[office], calendars=[meeting]))["events"]

    visit, inner = events
    assert visit["contains"] == [inner["id"]] and inner["inside"] == visit["id"]
    assert inner["calendar"]["title"] == "회의"
    assert "eventType" not in visit, "체류의 종류는 Timeline 이 고른다. 이동·산책만 코드가 정한다."


def test_prompt_sections_carry_no_raw_ids_or_coordinates():
    home, work = _home_work()
    meeting = calendar_item("m", "회의", start=_t("10:00"), end=_t("11:00"))
    chat = notification_item("chat", "카카오톡", "김민수")

    clues = _clues(stays=[home, work], calendars=[meeting], photos=[_photo("P1", "12:00", lat=37.6)])
    text = str(timeline_input_sections(clues))

    for raw_id in (home.raw_id, work.raw_id, chat.raw_id):
        assert raw_id not in text
    assert "37.6" not in text and "latitude" not in text


def test_bundled_notifications_carry_their_time():
    payment = _conversation("N1", "12:30", "12:31", kind=NotificationClueKind.PAYMENT)

    sections = timeline_input_sections(_clues(stays=_home_work(), notifications=[payment]))

    assert sections["notifications"] == []
    [entry] = [e for event in sections["events"] for e in event.get("notifications", [])]
    assert entry["firstAt"] == _at("12:30").isoformat() and entry["lastAt"] == _at("12:31").isoformat()


def test_day_notifications_carry_their_time_without_location():
    """위치가 없는 날은 알림이 그 자체로 event 이고, 받은 시각이 유일한 시간이다."""

    payment = _conversation("N1", "12:30", "12:31", kind=NotificationClueKind.PAYMENT)

    [entry] = timeline_input_sections(_clues(notifications=[payment]))["notifications"]

    assert entry["firstAt"] == _at("12:30").isoformat() and entry["lastAt"] == _at("12:31").isoformat()


def test_conversations_are_listed_apart_from_the_events():
    clues = _clues(stays=_home_work(), notifications=[_conversation("N1", "12:00", "12:05")])

    sections = timeline_input_sections(clues)

    assert [entry["id"] for entry in sections["conversations"]] == ["N1"]
    assert not any("conversations" in event for event in sections["events"])


@pytest.mark.parametrize("taken", ["10:00", "18:30"])
def test_a_photo_without_coordinates_outside_a_visit_is_its_own_event(taken):
    """좌표가 없으면 위치를 확인할 수 없어 시각이 방문 안일 때만 그 방문이다."""

    office = stay_item("office", start=_t("11:00"), end=_t("18:00"), places=["OO타워"])

    clues = _clues(stays=[office], photos=[_photo("P1", taken)])

    assert _base_of(clues, office.raw_id).photos == ()
