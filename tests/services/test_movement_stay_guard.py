"""이동 사이에 낀 장시간 체류 검사 (#119).

Location 전처리의 20분 기준은 프롬프트에 참고 값으로만 실렸다. Agent 가 그 기준을 어겨
몇 시간짜리 체류를 앞뒤 이동과 하나로 합쳐도 잡는 단계가 없었다. 여기서는 그 구조를
입력과 대조해 찾는지 본다.

**찾기만 한다.** 나누는 것은 Repair 가 한다. 그래서 event 가 나뉘거나 지워지지 않는 것도
함께 확인한다.
"""

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarningSeverity,
)
from app.services.location_metrics import SHORT_STAY_MAX
from app.services.movement_stay_guard import (
    find_movement_stay_violations,
    verify_movement_stay_boundary,
)
from tests.fixtures.requests import fixture_raw_id, make_request, movement_item, stay_item

DAY = "2026-06-20"

OUT = fixture_raw_id("move-out")
BACK = fixture_raw_id("move-back")
OFFICE = fixture_raw_id("stay-office")
STATION = fixture_raw_id("stay-station")


def _at(clock: str) -> str:
    return f"{DAY}T{clock}:00"


def _request(stay_start: str = "09:00", stay_end: str = "18:00", stay_id: str = "stay-office"):
    """집을 나서 → 머물고 → 돌아온 하루."""

    return make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=_at("08:20"), end=_at("09:00")),
            movement_item(2, raw_id="move-back", start=_at(stay_end), end=_at("19:00")),
        ],
        stays=[
            stay_item(
                1,
                raw_id=stay_id,
                start=_at(stay_start),
                end=_at(stay_end),
                place="회사",
            )
        ],
    )


def _event(*raw_ids: str, event_type=EventType.MOVEMENT, start="08:20", end="19:00"):
    return TimelineEventDraft(
        client_event_id="event-001",
        event_type=event_type,
        title="회사에 다녀옴",
        description="회사에 다녀왔어요.",
        start_time=f"{DAY}T{start}:00+09:00",
        end_time=f"{DAY}T{end}:00+09:00",
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            # 라벨은 일부러 틀리게 둔다. 검사는 rawId 로 입력을 찾는다.
            SourceRef(source_type=EventSourceType.MOVEMENT, raw_id=raw_id)
            for raw_id in raw_ids
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )


def _warnings(draft: TimelineDraft) -> list:
    return [
        warning
        for warning in draft.warnings
        if warning.warning_id.startswith("warning-movement-stay-")
    ]


# --- 기준 ----------------------------------------------------------------------


def test_threshold_is_the_location_metric_constant():
    """20분 기준은 Location 파생 지표의 것 하나다. 검사가 따로 갖지 않는다."""

    assert SHORT_STAY_MAX.total_seconds() == 20 * 60


def test_hours_long_stay_between_movements_is_a_violation():
    found = find_movement_stay_violations([OUT, OFFICE, BACK], _request())

    assert len(found) == 1
    assert found[0].stay.raw_id == OFFICE
    assert [span.raw_id for span in found[0].movements_before] == [OUT]
    assert [span.raw_id for span in found[0].movements_after] == [BACK]


def test_stay_of_exactly_twenty_minutes_is_not_a_violation():
    request = _request(stay_start="09:00", stay_end="09:20")

    assert find_movement_stay_violations([OUT, OFFICE, BACK], request) == []


def test_stay_of_twenty_one_minutes_is_a_violation():
    request = _request(stay_start="09:00", stay_end="09:21")

    assert len(find_movement_stay_violations([OUT, OFFICE, BACK], request)) == 1


def test_waiting_at_a_station_has_no_exception():
    """환승·대기도 20분을 넘으면 나눈다. 장소가 무엇인지는 보지 않는다."""

    request = make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=_at("08:00"), end=_at("09:00"), distance=80_000),
            movement_item(2, raw_id="move-back", start=_at("09:40"), end=_at("12:00"), distance=300_000),
        ],
        stays=[
            stay_item(
                1,
                raw_id="stay-station",
                start=_at("09:00"),
                end=_at("09:40"),
                place="서울역",
            )
        ],
    )

    found = find_movement_stay_violations([OUT, STATION, BACK], request)

    assert [violation.stay.place for violation in found] == ["서울역"]


# --- 구조 ----------------------------------------------------------------------


def test_stay_after_the_last_movement_is_not_between():
    """도착해서 머문 곳은 이동 사이가 아니다. 이동의 도착지 근거로 함께 인용할 수 있다."""

    assert find_movement_stay_violations([OUT, OFFICE], _request()) == []


def test_stay_before_the_first_movement_is_not_between():
    assert find_movement_stay_violations([OFFICE, BACK], _request()) == []


def test_long_stay_that_is_not_referenced_is_ignored():
    """event 가 근거로 대지 않은 체류는 그 event 의 구조가 아니다."""

    assert find_movement_stay_violations([OUT, BACK], _request()) == []


def test_detail_carries_the_long_stay_and_where_to_split():
    """Repair 가 같은 사실을 프롬프트에서 다시 계산하지 않게 넘긴다."""

    draft = _draft(_event(OUT, OFFICE, BACK))

    (found,) = verify_movement_stay_boundary(draft, _request())
    detail = found.detail()

    assert detail["eventStartTime"] == "2026-06-20T08:20:00+09:00"
    assert detail["eventEndTime"] == "2026-06-20T19:00:00+09:00"
    assert detail["longStays"] == [
        {
            "rawId": OFFICE,
            "startAt": "2026-06-20T09:00:00+09:00",
            "endAt": "2026-06-20T18:00:00+09:00",
            "minutes": 540,
            "place": "회사",
        }
    ]
    assert detail["segments"] == [
        {
            "kind": "MOVEMENT",
            "startAt": "2026-06-20T08:20:00+09:00",
            "endAt": "2026-06-20T09:00:00+09:00",
            "rawIds": [OUT],
        },
        {
            "kind": "STAY",
            "startAt": "2026-06-20T09:00:00+09:00",
            "endAt": "2026-06-20T18:00:00+09:00",
            "rawIds": [OFFICE],
            "place": "회사",
        },
        {
            "kind": "MOVEMENT",
            "startAt": "2026-06-20T18:00:00+09:00",
            "endAt": "2026-06-20T19:00:00+09:00",
            "rawIds": [BACK],
        },
    ]
    assert detail["outsideEventRawIds"] == []


def test_segments_stay_inside_the_event():
    """요청 시간 범위 끝에서 잘린 event 는 근거가 그 뒤까지 이어져 있다.

    근거의 시간을 그대로 주자 실제 LLM 은 event 밖으로 나가는 조각을 만들었고,
    나누기가 세 번 연속 거절됐다.
    """

    draft = _draft(_event(OUT, OFFICE, BACK, start="08:30", end="18:00"))

    (found,) = verify_movement_stay_boundary(draft, _request())
    detail = found.detail()

    assert [
        (segment["kind"], segment["startAt"][11:16], segment["endAt"][11:16])
        for segment in detail["segments"]
    ] == [("MOVEMENT", "08:30", "09:00"), ("STAY", "09:00", "18:00")]
    # 돌아오는 이동은 event 가 끝난 뒤에 시작한다. 조각을 만들 수 없다.
    assert detail["outsideEventRawIds"] == [BACK]
    # 걸린 체류는 근거 원본의 시간 그대로다.
    assert detail["longStays"][0]["endAt"] == "2026-06-20T18:00:00+09:00"


def test_short_stay_joins_the_movement_segment_around_it():
    """이어진 이동과 20분 이하 체류는 하나의 이동이다."""

    request = make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=_at("08:00"), end=_at("08:30")),
            movement_item(2, raw_id="move-mid", start=_at("08:40"), end=_at("09:00")),
            movement_item(3, raw_id="move-back", start=_at("18:00"), end=_at("19:00")),
        ],
        stays=[
            stay_item(1, raw_id="stay-station", start=_at("08:30"), end=_at("08:40"), place="환승역"),
            stay_item(2, raw_id="stay-office", start=_at("09:00"), end=_at("18:00"), place="회사"),
        ],
    )
    draft = _draft(
        _event(OUT, STATION, fixture_raw_id("move-mid"), OFFICE, BACK, start="08:00")
    )

    (found,) = verify_movement_stay_boundary(draft, request)

    assert [
        (segment["kind"], segment["startAt"][11:16], segment["endAt"][11:16], len(segment["rawIds"]))
        for segment in found.detail()["segments"]
    ] == [
        ("MOVEMENT", "08:00", "09:00", 3),
        ("STAY", "09:00", "18:00", 1),
        ("MOVEMENT", "18:00", "19:00", 1),
    ]


def _whole_day_request():
    """집에 있던 하루. 위치 수집이 끊겨 몇 분짜리 체류가 몇 시간 간격으로 들어온다."""

    return make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=_at("21:39"), end=_at("21:54")),
            movement_item(2, raw_id="move-back", start=_at("22:00"), end=_at("22:07")),
            movement_item(3, raw_id="move-late", start=_at("23:31"), end=_at("23:32")),
        ],
        stays=[
            stay_item(1, raw_id="stay-morning", start=_at("11:12"), end=_at("11:20"), place="집"),
            stay_item(2, raw_id="stay-noon", start=_at("14:26"), end=_at("14:36"), place="집"),
            stay_item(3, raw_id="stay-nearby", start=_at("21:54"), end=_at("22:00"), place="집 앞"),
            stay_item(4, raw_id="stay-office", start=_at("22:33"), end=_at("23:31"), place="집"),
            stay_item(5, raw_id="stay-station", start=_at("23:32"), end=_at("23:53"), place="집"),
        ],
    )


def _whole_day_event():
    return _event(
        fixture_raw_id("stay-morning"),
        fixture_raw_id("stay-noon"),
        OUT,
        fixture_raw_id("stay-nearby"),
        BACK,
        OFFICE,
        fixture_raw_id("move-late"),
        STATION,
        event_type=EventType.WORK,
        start="06:50",
        end="23:53",
    )


def test_sources_hours_apart_are_not_one_movement():
    """실제 draft 에서 오전의 8분짜리 체류와 밤의 이동이 11시간짜리 이동 하나가 됐다.

    한 event 가 하루의 위치 기록을 거의 다 근거로 댄 경우다. 수집이 끊긴 구간을 사이에 둔
    근거는 이어진 이동이 아니다.
    """

    (found,) = verify_movement_stay_boundary(_draft(_whole_day_event()), _whole_day_request())

    assert [
        (segment["kind"], segment["startAt"][11:16], segment["endAt"][11:16], len(segment["rawIds"]))
        for segment in found.detail()["segments"]
    ] == [
        ("MOVEMENT", "21:39", "22:07", 3),  # 이동 → 6분 체류 → 이동
        ("STAY", "22:33", "23:31", 1),
        ("MOVEMENT", "23:31", "23:32", 1),
        ("STAY", "23:32", "23:53", 1),
    ]


def test_short_stay_away_from_any_movement_is_not_a_place_to_split():
    """이동과 이어지지 않은 짧은 체류로 조각을 만들면 몇 분짜리 체류 카드가 된다."""

    (found,) = verify_movement_stay_boundary(_draft(_whole_day_event()), _whole_day_request())
    detail = found.detail()

    assert [
        (stay["rawId"], stay["startAt"][11:16], stay["endAt"][11:16], stay["place"])
        for stay in detail["shortStays"]
    ] == [
        (fixture_raw_id("stay-morning"), "11:12", "11:20", "집"),
        (fixture_raw_id("stay-noon"), "14:26", "14:36", "집"),
    ]
    in_segments = {raw_id for segment in detail["segments"] for raw_id in segment["rawIds"]}
    assert fixture_raw_id("stay-morning") not in in_segments
    assert fixture_raw_id("stay-noon") not in in_segments


def test_short_stays_are_omitted_when_there_are_none():
    (found,) = verify_movement_stay_boundary(_draft(_event(OUT, OFFICE, BACK)), _request())

    assert "shortStays" not in found.detail()


def test_movements_across_a_collection_gap_are_separate_segments():
    """45분 넘게 기록이 없으면 두 이동은 이어진 것이 아니다."""

    request = make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=_at("08:00"), end=_at("08:30")),
            movement_item(2, raw_id="move-mid", start=_at("10:00"), end=_at("10:30")),
            movement_item(3, raw_id="move-back", start=_at("18:00"), end=_at("19:00")),
        ],
        stays=[
            stay_item(1, raw_id="stay-office", start=_at("10:30"), end=_at("18:00"), place="회사"),
        ],
    )
    draft = _draft(
        _event(OUT, fixture_raw_id("move-mid"), OFFICE, BACK, start="08:00")
    )

    (found,) = verify_movement_stay_boundary(draft, request)

    assert [
        (segment["kind"], segment["startAt"][11:16], segment["endAt"][11:16])
        for segment in found.detail()["segments"]
    ] == [
        ("MOVEMENT", "08:00", "08:30"),
        ("MOVEMENT", "10:00", "10:30"),
        ("STAY", "10:30", "18:00"),
        ("MOVEMENT", "18:00", "19:00"),
    ]


# --- draft 검사 -----------------------------------------------------------------


def test_movement_event_is_checked_even_though_duration_guard_skips_it():
    draft = _draft(_event(OUT, OFFICE, BACK, event_type=EventType.MOVEMENT))

    found = verify_movement_stay_boundary(draft, _request())

    assert len(found) == 1
    (warning,) = _warnings(draft)
    assert warning.severity is TimelineWarningSeverity.HIGH
    assert "540분" in warning.message
    assert {ref.raw_id for ref in warning.source_refs} == {OUT, OFFICE, BACK}


def test_other_event_types_are_checked_too():
    draft = _draft(_event(OUT, OFFICE, BACK, event_type=EventType.WORK))

    assert len(verify_movement_stay_boundary(draft, _request())) == 1


def test_guard_does_not_split_or_remove_events():
    draft = _draft(_event(OUT, OFFICE, BACK))
    before = draft.events[0].model_copy(deep=True)

    verify_movement_stay_boundary(draft, _request())

    assert draft.events == [before]


def test_short_stay_inside_a_journey_is_left_alone():
    """짧은 센서 분절을 낀 정상적인 연속 이동은 유지한다."""

    request = _request(stay_start="09:00", stay_end="09:10")
    draft = _draft(_event(OUT, OFFICE, BACK))

    assert verify_movement_stay_boundary(draft, request) == []
    assert _warnings(draft) == []


def test_repeated_runs_do_not_accumulate():
    draft = _draft(_event(OUT, OFFICE, BACK))

    verify_movement_stay_boundary(draft, _request())
    verify_movement_stay_boundary(draft, _request())

    assert len(_warnings(draft)) == 1


def test_warning_disappears_after_the_event_is_split():
    draft = _draft(_event(OUT, OFFICE, BACK))
    verify_movement_stay_boundary(draft, _request())
    assert _warnings(draft)

    # Repair 가 앞 이동·체류·뒤 이동으로 나눈 상황.
    draft.events = [
        _event(OUT, start="08:20", end="09:00"),
        _event(OFFICE, event_type=EventType.WORK, start="09:00", end="18:00"),
        _event(BACK, start="18:00", end="19:00"),
    ]

    assert verify_movement_stay_boundary(draft, _request()) == []
    assert _warnings(draft) == []
