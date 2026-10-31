"""근거 묶음 검증 (#169). 붙이는 규칙은 사용자 결정(2026-10-29)이다."""

from datetime import datetime, timedelta, timezone

from app.schemas import EventSourceType
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import (
    build_timeline_clues,
    expand_evidence,
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


def _photo(label, hhmm, *, lat=None, lon=127.0, places=()):
    return PhotoClue(
        clue_id=label,
        raw_id=fixture_raw_id(f"photo-{label}"),
        taken_at=_at(hhmm),
        latitude=lat,
        longitude=lon if lat is not None else None,
        places=list(places),
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


def _clues(stays=(), calendars=(), photos=(), notifications=()):
    request = make_request(stays=list(stays), calendars=list(calendars))
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


def test_calendar_spanning_two_segments_is_evidence_for_both():
    home, work = _home_work()
    meeting = calendar_item("m", "아침 회의", start=_t("08:00"), end=_t("10:00"))

    clues = _clues(stays=[home, work], calendars=[meeting])

    first, second = [b for b in clues.bundles if b.segment]
    assert [c.title for c in first.calendars] == ["아침 회의"]
    assert [c.title for c in second.calendars] == ["아침 회의"]


def test_background_calendar_is_its_own_bundle_and_marks_the_segments_inside():
    home, work = _home_work()
    trip = calendar_item("trip", "출장", start=_t("06:00"), end=_t("20:00"))

    clues = _clues(stays=[home, work], calendars=[trip])

    own = [b for b in clues.bundles if b.segment is None]
    assert [c.title for b in own for c in b.calendars] == ["출장"]
    assert all(b.background_calendar_ids == ["C1"] for b in clues.bundles if b.segment)


def test_calendar_with_no_segment_is_its_own_bundle():
    meeting = calendar_item("m", "온라인 회의", start=_t("21:00"), end=_t("22:00"))

    clues = _clues(calendars=[meeting])

    [bundle] = clues.bundles
    assert bundle.segment is None and [c.title for c in bundle.calendars] == ["온라인 회의"]


def test_photo_goes_to_the_segment_that_holds_its_time():
    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "12:00")])

    work_bundle = next(b for b in clues.bundles if b.segment and work.raw_id in b.segment.raw_ids)
    assert [p.clue_id for p in work_bundle.photos] == ["P1"]


def test_photo_at_a_boundary_also_goes_to_the_neighbor_at_the_same_spot():
    home, work = _home_work()
    leaving = _photo("P1", "08:29", lat=37.6001)

    clues = _clues(stays=[home, work], photos=[leaving])

    holders = [b for b in clues.bundles if any(p.clue_id == "P1" for p in b.photos)]
    assert len(holders) == 2


def test_photo_in_an_empty_time_goes_to_the_segment_at_its_spot():
    home, work = _home_work()
    on_the_way_in = _photo("P1", "09:00", lat=37.6001)

    clues = _clues(stays=[home, work], photos=[on_the_way_in])

    [holder] = [b for b in clues.bundles if any(p.clue_id == "P1" for p in b.photos)]
    assert work.raw_id in holder.segment.raw_ids


def test_photo_with_no_matching_segment_is_its_own_bundle_not_the_nearest():
    """가장 가까운 구간에 붙이지 않는다(#163)."""

    home, work = _home_work()
    dinner = _photo("P1", "22:56", lat=37.9)

    clues = _clues(stays=[home, work], photos=[dinner])

    [holder] = [b for b in clues.bundles if any(p.clue_id == "P1" for p in b.photos)]
    assert holder.segment is None


def test_conversation_attaches_to_every_overlapping_segment_but_other_notifications_do_not():
    home, work = _home_work()
    chat = _conversation("N1", "08:00", "10:00")
    payment = _conversation("N2", "12:30", "12:30", kind=NotificationClueKind.PAYMENT)

    clues = _clues(stays=[home, work], notifications=[chat, payment])

    holders = [b for b in clues.bundles if b.conversations]
    assert len(holders) == 2
    assert [c.clue_id for c in clues.day_notifications] == ["N2"]
    assert not any(b.conversations and b.conversations[0].clue_id == "N2" for b in clues.bundles)


def test_no_location_day_still_bundles_photos_and_calendars():
    meeting = calendar_item("m", "회의", start=_t("10:00"), end=_t("11:00"))

    clues = _clues(calendars=[meeting], photos=[_photo("P1", "12:00")])

    assert not clues.skeleton.has_location
    assert [b.bundle_id for b in clues.bundles] == ["B1", "B2"]
    assert timeline_input_sections(clues)["day"] == {"hasLocation": False}


def test_bundle_ids_follow_time_order():
    home, work = _home_work()

    clues = _clues(stays=[home, work], photos=[_photo("P1", "23:00")])

    assert [b.bundle_id for b in clues.bundles] == ["B1", "B2", "B3"]
    assert clues.bundles[-1].photos[0].clue_id == "P1"


def test_expand_bundle_gives_segment_and_attached_clues_except_those_taken_directly():
    home, work = _home_work()
    lunch = _photo("P1", "12:00")
    office = _photo("P2", "15:00")
    request = make_request(
        stays=[home, work],
        photos=[
            photo_item("P1", taken=_t("12:00"), raw_id="photo-P1"),
            photo_item("P2", taken=_t("15:00"), raw_id="photo-P2"),
        ],
    )
    clues = build_timeline_clues(build_location_skeleton(request), [], [lunch, office], [])
    work_bundle = next(b for b in clues.bundles if b.segment and work.raw_id in b.segment.raw_ids)

    work_refs, lunch_refs = expand_evidence(clues, [[work_bundle.bundle_id], ["P1"]], request)

    assert [r.raw_id for r in work_refs] == [work.raw_id, office.raw_id]
    assert work_refs[0].source_type is EventSourceType.STAY
    assert work_refs[0].reason == "위치 구간"
    assert [r.raw_id for r in lunch_refs] == [lunch.raw_id]


def test_expand_ignores_unknown_ids_and_dedupes():
    home, _ = _home_work()
    request = make_request(stays=[home])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])

    [refs] = expand_evidence(clues, [["B1", "B1", "B9", "P7"]], request)

    assert [r.raw_id for r in refs] == [home.raw_id]


def test_prompt_sections_carry_no_raw_ids_or_coordinates():
    home, work = _home_work()
    meeting = calendar_item("m", "회의", start=_t("10:00"), end=_t("11:00"))
    chat = notification_item("chat", "카카오톡", "김민수")

    clues = _clues(stays=[home, work], calendars=[meeting], photos=[_photo("P1", "12:00", lat=37.6)])
    text = str(timeline_input_sections(clues))

    for raw_id in (home.raw_id, work.raw_id, chat.raw_id):
        assert raw_id not in text
    assert "37.6" not in text and "latitude" not in text


def test_day_notifications_carry_no_time_on_a_located_day():
    payment = _conversation("N1", "12:30", "12:30", kind=NotificationClueKind.PAYMENT)

    [entry] = timeline_input_sections(_clues(stays=_home_work(), notifications=[payment]))["notifications"]

    assert "firstAt" not in entry and "lastAt" not in entry


def test_day_notifications_carry_their_time_without_location():
    """위치가 없는 날은 알림이 그 자체로 event 이고, 받은 시각이 유일한 시간이다."""

    payment = _conversation("N1", "12:30", "12:31", kind=NotificationClueKind.PAYMENT)

    [entry] = timeline_input_sections(_clues(notifications=[payment]))["notifications"]

    assert entry["firstAt"] == _at("12:30").isoformat() and entry["lastAt"] == _at("12:31").isoformat()
