"""위치 구간 복원 (#167, v4). 뼈대 구간의 시간은 최종 event 로 덮인다."""

from datetime import datetime, timedelta, timezone

from app.schemas import EventSourceType, EventType, InferenceLevel, SourceRef, TimelineDraft, TimelineEventDraft
from app.services.clue_bundles import build_timeline_clues
from app.services.location_skeleton import build_location_skeleton
from app.services.skeleton_guard import ensure_skeleton_segments
from tests.fixtures.requests import fixture_raw_id, make_request, photo_item, stay_item

_KST = timezone(timedelta(hours=9))


def _setup():
    home = stay_item("home", lat=37.5, start="2026-06-20T06:00:00", end="2026-06-20T10:00:00",
                     places=["한빛아파트"], address="서울 마포구 도화동 17-14")
    office = stay_item("office", lat=37.6, start="2026-06-20T11:00:00", end="2026-06-20T18:00:00")
    request = make_request(stays=[home, office])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    return request, clues, home, office


def _work_event(office) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id="event-001", event_type=EventType.WORK, title="업무",
        description="{place}에서 일했다.",
        start_time=datetime(2026, 6, 20, 11, 0, tzinfo=_KST), end_time=datetime(2026, 6, 20, 18, 0, tzinfo=_KST),
        confidence=0.8, inference_level=InferenceLevel.DIRECT,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id)],
    )


def test_segment_no_event_uses_is_restored_as_a_plain_stay():
    request, clues, home, office = _setup()
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[_work_event(office)])

    ensure_skeleton_segments(draft, request, clues)

    restored = next(e for e in draft.events if home.raw_id in {r.raw_id for r in e.source_refs})
    assert restored.event_type is EventType.UNKNOWN
    assert restored.title == "마포구 도화동에서 보낸 시간", "되살린 틀 제목은 그 장소의 지명(구 + 동)을 쓴다."
    assert restored.description == "{place}에서 시간을 보냈다."
    assert restored.place_candidates == ["한빛아파트"]
    assert any("기본 event" in w.message for w in draft.warnings)


def test_segment_whose_time_is_covered_is_left_alone():
    request, clues, home, office = _setup()
    morning = _work_event(office)
    morning.source_refs = [SourceRef(source_type=EventSourceType.STAY, raw_id=home.raw_id)]
    morning.start_time = datetime(2026, 6, 20, 6, 0, tzinfo=_KST)
    morning.end_time = datetime(2026, 6, 20, 10, 0, tzinfo=_KST)
    draft = TimelineDraft(
        user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[morning, _work_event(office)]
    )

    ensure_skeleton_segments(draft, request, clues)

    assert len(draft.events) == 2


def test_segment_cited_but_only_partly_covered_gets_the_rest_back():
    """근거로만 대고 시간을 일부만 쓴 event 가 있으면 남은 시간을 되살린다(재실행에서 본 것)."""

    request, clues, home, office = _setup()
    lunch_only = _work_event(office)
    lunch_only.end_time = datetime(2026, 6, 20, 12, 0, tzinfo=_KST)
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[lunch_only])

    ensure_skeleton_segments(draft, request, clues)

    restored = [e for e in draft.events if e.merge_reason and "틀 문장" in e.merge_reason]
    assert {(e.start_time.hour, e.end_time.hour) for e in restored} == {(6, 10), (12, 18)}


def test_movement_without_a_movement_record_is_removed_and_its_stay_comes_back():
    """기록되지 않은 이동으로 만든 MOVEMENT 가 체류를 가져가면 체류가 사라졌다(재실행에서 본 것)."""

    from app.services.skeleton_guard import remove_unrecorded_movements

    request, clues, home, office = _setup()
    fake = TimelineEventDraft(
        client_event_id="event-002", event_type=EventType.MOVEMENT, title="이동", description="",
        start_time=datetime(2026, 6, 20, 10, 0, tzinfo=_KST), end_time=datetime(2026, 6, 20, 11, 0, tzinfo=_KST),
        confidence=0.6, inference_level=InferenceLevel.INFERRED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=home.raw_id)],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[_work_event(office), fake])

    remove_unrecorded_movements(draft, request, clues)
    ensure_skeleton_segments(draft, request, clues)

    assert not any(e.event_type is EventType.MOVEMENT for e in draft.events)
    assert any(home.raw_id in {r.raw_id for r in e.source_refs} for e in draft.events)


def test_movement_records_are_taken_out_of_a_stay_event_and_the_movement_comes_back():
    from app.schemas import GeoPlace, MovementItem
    from app.services.skeleton_guard import separate_movement_segments
    from tests.fixtures.requests import fixture_raw_id

    home = stay_item("home", lat=37.5, start="2026-06-20T06:00:00", end="2026-06-20T10:00:00")
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-c"), start_at="2026-06-20T10:00:00", end_at="2026-06-20T11:00:00",
        start=GeoPlace(latitude=37.5, longitude=127.0), end=GeoPlace(latitude=37.6, longitude=127.0),
        transports=["IN_VEHICLE"],
    )
    request = make_request(stays=[home], movements=[commute])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    merged = TimelineEventDraft(
        client_event_id="event-001", event_type=EventType.WORK, title="오전 작업", description="",
        start_time=datetime(2026, 6, 20, 6, 0, tzinfo=_KST), end_time=datetime(2026, 6, 20, 11, 0, tzinfo=_KST),
        confidence=0.6, inference_level=InferenceLevel.INFERRED,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=home.raw_id),
            SourceRef(source_type=EventSourceType.MOVEMENT, raw_id=commute.raw_id),
        ],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[merged])

    separate_movement_segments(draft, request, clues)
    ensure_skeleton_segments(draft, request, clues)

    work = next(e for e in draft.events if e.event_type is EventType.WORK)
    assert [r.raw_id for r in work.source_refs] == [home.raw_id]
    assert work.end_time.hour == 10, "이동 구간에 걸친 끝을 자른다."
    assert any(e.event_type is EventType.MOVEMENT for e in draft.events)


def test_event_outside_its_evidence_time_is_removed():
    from app.services.skeleton_guard import remove_events_outside_evidence

    request, clues, home, office = _setup()
    night = TimelineEventDraft(
        client_event_id="event-009", event_type=EventType.UNKNOWN, title="밤", description="밤을 보냈다.",
        start_time=datetime(2026, 6, 20, 19, 0, tzinfo=_KST), end_time=datetime(2026, 6, 21, 0, 0, tzinfo=_KST),
        confidence=0.4, inference_level=InferenceLevel.INFERRED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id)],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[_work_event(office), night])

    remove_events_outside_evidence(draft, request, clues)

    assert [e.title for e in draft.events] == ["업무"]


def test_event_without_location_evidence_is_cut_to_its_evidence_time():
    """위치 기록이 끊긴 뒤를 저녁 사진 두 장으로 8시간 `밤` event 로 만든 실제 Timeline 출력."""

    from app.services.skeleton_guard import remove_events_outside_evidence

    office = stay_item("office", lat=37.6, start="2026-06-20T11:00:00", end="2026-06-20T22:00:00")
    dinner = [
        photo_item("d1", taken="2026-06-20T22:56:00", lat=None, lon=None),
        photo_item("d2", taken="2026-06-20T22:57:00", lat=None, lon=None),
    ]
    request = make_request(stays=[office], photos=dinner)
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    night = TimelineEventDraft(
        client_event_id="event-009", event_type=EventType.UNKNOWN, title="밤", description="밤을 보냈다.",
        start_time=datetime(2026, 6, 20, 22, 0, tzinfo=_KST), end_time=datetime(2026, 6, 21, 6, 0, tzinfo=_KST),
        confidence=0.4, inference_level=InferenceLevel.INFERRED,
        source_refs=[SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id) for photo in dinner],
    )
    work = _work_event(office)
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[work, night])

    remove_events_outside_evidence(draft, request, clues)

    assert (night.start_time, night.end_time) == (
        datetime(2026, 6, 20, 22, 36, tzinfo=_KST),
        datetime(2026, 6, 20, 23, 17, tzinfo=_KST),
    ), "사진 시각 앞뒤 20분 밖은 근거가 덮지 않는다."
    assert (work.start_time, work.end_time) == (
        datetime(2026, 6, 20, 11, 0, tzinfo=_KST),
        datetime(2026, 6, 20, 18, 0, tzinfo=_KST),
    ), "위치 기록이 있는 event 는 자르지 않는다."


def test_located_event_is_cut_to_where_its_records_put_the_user():
    """사무실 체류를 근거로 대고 그 뒤 빈 시간에 만든 밤 event. 메시지는 그곳에 있었다는 근거가 아니다."""

    from app.schemas import NotificationItem
    from app.schemas.clues import NotificationClue, NotificationClueKind
    from app.services.skeleton_guard import remove_events_outside_evidence

    office = stay_item("office", lat=37.6, start="2026-06-20T11:00:00", end="2026-06-20T22:00:00")
    chat_id = fixture_raw_id("chat-night")
    chat = NotificationItem(
        rawId=chat_id, postedAt="2026-06-20T23:30:00+09:00", appName="카카오톡", title="민수", text="잘 자",
    )
    request = make_request(stays=[office], notifications=[chat])
    clue = NotificationClue(
        clue_id="N1", kind=NotificationClueKind.CONVERSATION, counterpart="민수", summary="인사", raw_ids=[chat_id],
    )
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [clue])
    night = TimelineEventDraft(
        client_event_id="event-009", event_type=EventType.UNKNOWN, title="밤", description="밤을 보냈다.",
        start_time=datetime(2026, 6, 20, 22, 0, tzinfo=_KST), end_time=datetime(2026, 6, 21, 6, 0, tzinfo=_KST),
        confidence=0.4, inference_level=InferenceLevel.INFERRED,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id),
            SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=chat_id),
        ],
    )
    work = _work_event(office)
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[work, night])

    remove_events_outside_evidence(draft, request, clues)

    assert draft.events == [work]


def test_photo_taken_hours_away_is_detached_and_the_stay_goes_back_to_its_template():
    """주소 없는 오전 사진을 다섯 시간 뒤의 체류에 붙인 실제 Timeline 출력."""

    from app.services.skeleton_guard import remove_events_outside_evidence

    stay = stay_item("stay", lat=37.5, start="2026-06-20T16:59:00", end="2026-06-20T17:38:00",
                     address="전남 여수시 덕충동 2079")
    morning = photo_item("morning", taken="2026-06-20T11:24:00", lat=None, lon=None)
    request = make_request(stays=[stay], photos=[morning])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    work = TimelineEventDraft(
        client_event_id="event-001", event_type=EventType.WORK, title="덕충동에서 보수 작업",
        description="{place}에서 작업을 했다.",
        start_time=datetime(2026, 6, 20, 16, 59, tzinfo=_KST), end_time=datetime(2026, 6, 20, 17, 38, tzinfo=_KST),
        confidence=0.7, inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=stay.raw_id),
            SourceRef(source_type=EventSourceType.PHOTO, raw_id=morning.raw_id),
        ],
        base_event_ids=["E1"],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[work])

    remove_events_outside_evidence(draft, request, clues)

    assert [ref.raw_id for ref in work.source_refs] == [stay.raw_id]
    assert (work.event_type, work.title, work.description) == (
        EventType.UNKNOWN, "여수시 덕충동에서 보낸 시간", "{place}에서 시간을 보냈다."
    )


def test_event_spanning_a_whole_movement_keeps_the_longer_side():
    """집과 사무실 체류를 이동을 사이에 두고 06:00~22:00 하나로 합친 실제 Timeline 출력."""

    from app.schemas import GeoPlace, MovementItem
    from app.services.skeleton_guard import separate_movement_segments
    from tests.fixtures.requests import fixture_raw_id

    home = stay_item("home", lat=37.27, start="2026-06-20T06:00:00", end="2026-06-20T10:19:00")
    office = stay_item("office", lat=37.54, start="2026-06-20T11:25:00", end="2026-06-20T22:00:00")
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at="2026-06-20T10:19:00", end_at="2026-06-20T11:25:00",
        start=GeoPlace(latitude=37.27, longitude=127.0), end=GeoPlace(latitude=37.54, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=40000,
    )
    request = make_request(stays=[home, office], movements=[commute])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    merged = TimelineEventDraft(
        client_event_id="event-001", event_type=EventType.UNKNOWN, title="긴 체류", description="{place}에서 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 6, 0, tzinfo=_KST), end_time=datetime(2026, 6, 20, 22, 0, tzinfo=_KST),
        confidence=0.5, inference_level=InferenceLevel.DIRECT,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=home.raw_id),
            SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id),
        ],
        base_event_ids=["E1", "E3"],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[merged])

    separate_movement_segments(draft, request, clues)
    ensure_skeleton_segments(draft, request, clues)

    assert (merged.start_time.hour, merged.start_time.minute) == (11, 25)
    assert [ref.raw_id for ref in merged.source_refs] == [office.raw_id]
    assert merged.base_event_ids == ["E3"]
    restored = {(e.start_time.hour, e.end_time.hour) for e in draft.events if e is not merged}
    assert restored == {(6, 10), (10, 11)}, "집 체류와 이동은 틀 문장으로 되살린다."


def test_event_that_was_only_the_movement_is_removed():
    """출근 이동(10:19~11:25)을 `업무` 로 채운 실제 Timeline 출력. 이동 구간을 빼면 남는 시간이 없다."""

    from app.schemas import GeoPlace, MovementItem
    from app.services.skeleton_guard import separate_movement_segments

    home = stay_item("home", lat=37.27, start="2026-06-20T06:00:00", end="2026-06-20T10:19:54")
    office = stay_item("office", lat=37.54, start="2026-06-20T11:25:36", end="2026-06-20T22:00:00")
    # 기록은 초 단위(10:19:54)이고 Timeline 은 분 단위(10:19)로 쓴다 — 실제 trace 의 경계다.
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at="2026-06-20T10:19:54", end_at="2026-06-20T11:25:36",
        start=GeoPlace(latitude=37.27, longitude=127.0), end=GeoPlace(latitude=37.54, longitude=127.0),
        transports=["IN_VEHICLE"], distance_meters=40000,
    )
    chat = photo_item("desk", taken="2026-06-20T10:40:00", lat=None, lon=None)
    request = make_request(stays=[home, office], movements=[commute], photos=[chat])
    clues = build_timeline_clues(build_location_skeleton(request), [], [], [])
    work = TimelineEventDraft(
        client_event_id="event-002", event_type=EventType.WORK, title="업무", description="업무를 했다.",
        start_time=datetime(2026, 6, 20, 10, 19, tzinfo=_KST), end_time=datetime(2026, 6, 20, 11, 25, tzinfo=_KST),
        confidence=0.5, inference_level=InferenceLevel.INFERRED,
        source_refs=[
            SourceRef(source_type=EventSourceType.MOVEMENT, raw_id=commute.raw_id),
            SourceRef(source_type=EventSourceType.PHOTO, raw_id=chat.raw_id),
        ],
        base_event_ids=["E2"],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[work])

    separate_movement_segments(draft, request, clues)

    assert draft.events == []
