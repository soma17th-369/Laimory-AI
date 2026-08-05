"""TimelineDraft → 결과 저장 요청 변환 검증."""

from datetime import datetime, timedelta, timezone

from app.schemas import EventSourceType, EventType, InferenceLevel, SourceRef
from app.schemas.timeline import TimelineDraft, TimelineEventDraft
from app.services.narrative_guard import DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH
from app.services.timeline_result import build_result_request
from tests.fixtures.requests import fixture_raw_id

_KST = timezone(timedelta(hours=9))


def _event(**overrides) -> TimelineEventDraft:
    defaults = dict(
        client_event_id="evt-1",
        event_type=EventType.MEAL,
        title="점심",
        description="근처 식당에서 식사",
        start_time=datetime(2026, 7, 22, 12, 0, tzinfo=_KST),
        end_time=datetime(2026, 7, 22, 13, 0, tzinfo=_KST),
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(
                source_type=EventSourceType.STAY,
                raw_id=fixture_raw_id("result-1"),
            )
        ],
    )
    defaults.update(overrides)
    return TimelineEventDraft(**defaults)


def _draft(*events: TimelineEventDraft) -> TimelineDraft:
    return TimelineDraft(
        user_id="u-1",
        date="2026-07-22",
        timezone="Asia/Seoul",
        events=list(events),
    )


def test_maps_draft_event_to_contract_fields():
    request = build_result_request(_draft(_event()))

    [event] = request.events
    assert event.event_type is EventType.MEAL
    assert event.title == "점심"
    assert event.subtitle == "근처 식당에서 식사"
    assert event.source_raw_ids == [fixture_raw_id("result-1")]


def test_serialized_body_matches_the_contract_shape():
    body = build_result_request(_draft(_event())).model_dump(
        by_alias=True, mode="json"
    )

    assert set(body) == {"events"}
    assert set(body["events"][0]) == {
        "eventType",
        "title",
        "subtitle",
        "startAt",
        "endAt",
        # 내부에서 확정한 장소를 저장까지 가져간다(#67).
        "placeLabel",
        "address",
        "sourceRawIds",
    }
    assert body["events"][0]["startAt"] == "2026-07-22T12:00:00+09:00"


def test_place_is_carried_to_the_result_contract():
    event = _event()
    event.place_label = "집"
    event.address = "경기도 오산시 운암로 90"

    [result] = build_result_request(_draft(event)).events

    assert result.place_label == "집"
    assert result.address == "경기도 오산시 운암로 90"


def test_missing_place_is_null_not_invented():
    [result] = build_result_request(_draft(_event())).events

    assert result.place_label is None
    assert result.address is None


def test_empty_description_becomes_null_subtitle():
    request = build_result_request(_draft(_event(description="   ")))

    assert request.events[0].subtitle is None


def test_duplicate_source_refs_are_deduped_in_order():
    raw_a = fixture_raw_id("result-a")
    raw_b = fixture_raw_id("result-b")
    event = _event(
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=raw_a),
            SourceRef(source_type=EventSourceType.PHOTO, raw_id=raw_b),
            SourceRef(source_type=EventSourceType.CALENDAR, raw_id=raw_a),
        ]
    )

    request = build_result_request(_draft(event))

    assert request.events[0].source_raw_ids == [raw_a, raw_b]


def test_long_title_and_subtitle_are_truncated_to_the_product_limits():
    # 저장 컬럼 한도(255자)가 아니라 제품이 약속한 30/120자가 정본이다(#67).
    request = build_result_request(
        _draft(_event(title="가" * 300, description="나" * 300))
    )

    assert len(request.events[0].title) == TITLE_MAX_LENGTH
    assert len(request.events[0].subtitle) == DESCRIPTION_MAX_LENGTH


def test_truncation_prefers_a_sentence_boundary():
    # 끊을 수 있으면 문장 끝에서 끊는다. 말이 잘린 채 사용자에게 가지 않게 한다.
    description = "아침에 카페에서 커피를 마셨어요. " + "그 뒤로 오래 걸었어요. " * 8
    request = build_result_request(_draft(_event(description=description)))

    subtitle = request.events[0].subtitle
    assert len(subtitle) <= DESCRIPTION_MAX_LENGTH
    assert subtitle.endswith(".")


def test_times_are_converted_to_draft_timezone():
    utc_event = _event(
        start_time=datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc),
    )

    body = build_result_request(_draft(utc_event)).model_dump(
        by_alias=True, mode="json"
    )

    assert body["events"][0]["startAt"] == "2026-07-22T12:00:00+09:00"


def test_empty_draft_still_produces_a_request():
    """'결과 없음' 도 확정된 결과다. 저장 요청을 건너뛰지 않는다."""

    assert build_result_request(_draft()).events == []
