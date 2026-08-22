"""장소 출력(place/address) 확정 검증.

place 은 실제 장소명이거나 친숙한 생활 장소여야 하고, address 는 입력 근거에
실제 주소 문자열이 있을 때만 채워야 한다. LLM 이 사진에서 읽어 낸 상호명처럼 코드가
알 수 없는 값은 덮어쓰지 않는다.
"""

import pytest

from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    GeoPlace,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarningSeverity,
    UserMemory,
)
from app.services.place_resolver import (
    calendar_place_label,
    is_exact_address,
    is_vague_place_label,
    resolve_candidate_places,
    resolve_places,
)
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    movement_item,
    photo_item,
    stay_item,
)

APARTMENT = "오산운암3단지 주공아파트"
HOME_ADDRESS = "경기도 오산시 운암로 90"
HOME_LOCATION_TEXT = f"집({HOME_ADDRESS})"

STAY_REF = (EventSourceType.STAY, "stay-1")
MOVE_REF = (EventSourceType.MOVEMENT, "move-1")
CALENDAR_REF = (EventSourceType.CALENDAR, "cal-1")
PHOTO_REF = (EventSourceType.PHOTO, "photo-1")


def _request():
    movement = movement_item(2, raw_id="move-1")
    movement.start = GeoPlace(place="출발 아파트", address="경기도 오산시 운암로 90")
    movement.end = GeoPlace(place="도착 공원", address="경기도 오산시 원동 500")
    return make_request(
        stays=[stay_item(1, raw_id="stay-1", place=APARTMENT, address=HOME_ADDRESS, places=[])],
        movements=[movement],
        calendars=[calendar_item(3, "개발", raw_id="cal-1", location_text=HOME_LOCATION_TEXT)],
        photos=[photo_item(4, raw_id="photo-1")],
    )


def _event(*refs, place=None, address=None, title="이벤트") -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id="event-001",
        event_type=EventType.REST,
        title=title,
        start_time="2026-06-20T09:00:00+09:00",
        end_time="2026-06-20T10:00:00+09:00",
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        place=place,
        address=address,
        source_refs=[
            SourceRef(source_type=st, raw_id=fixture_raw_id(raw_id))
            for st, raw_id in refs
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


# --- 얼버무림 판정 -------------------------------------------------------------


@pytest.mark.parametrize("label", ["한 곳", "한곳", "근처", "주변", "어떤 장소", "  ", None])
def test_vague_labels_are_not_places(label):
    assert is_vague_place_label(label)


@pytest.mark.parametrize("label", ["집", "두꺼비 감자탕 지산점", APARTMENT, "서울드래곤시티"])
def test_real_place_names_are_not_vague(label):
    assert not is_vague_place_label(label)


# --- 캘린더 라벨 추출 ----------------------------------------------------------


def test_calendar_label_strips_the_address_in_parentheses():
    assert calendar_place_label(HOME_LOCATION_TEXT) == "집"


def test_calendar_label_without_parentheses_is_used_whole():
    assert calendar_place_label("용산 서울드래곤시티") == "용산 서울드래곤시티"
    assert calendar_place_label(None) is None


# --- place ---------------------------------------------------------------


def test_missing_place_is_filled_from_the_stay_place():
    draft = _draft(_event(STAY_REF))

    resolve_places(draft, _request())

    assert draft.events[0].place == APARTMENT


def test_vague_place_is_replaced_by_the_evidence_place():
    draft = _draft(_event(STAY_REF, place="한 곳"))

    resolve_places(draft, _request())

    assert draft.events[0].place == APARTMENT


def test_vague_place_without_any_evidence_is_cleared_not_invented():
    draft = _draft(_event(PHOTO_REF, place="근처"))

    resolve_places(draft, _request())

    # 사진에는 place 필드가 없다. 얼버무림을 남기느니 비운다.
    assert draft.events[0].place is None


def test_a_place_name_read_from_a_photo_is_never_overwritten():
    # `배스킨라빈스` 는 어떤 입력 필드에도 없다. vision 이 영수증에서 읽은 값이다.
    draft = _draft(_event(STAY_REF, PHOTO_REF, place="배스킨라빈스"))

    resolve_places(draft, _request())

    assert draft.events[0].place == "배스킨라빈스"


def test_stay_place_wins_over_movement_and_calendar():
    draft = _draft(_event(CALENDAR_REF, MOVE_REF, STAY_REF))

    resolve_places(draft, _request())

    assert draft.events[0].place == APARTMENT


def test_movement_prefers_its_destination_place():
    draft = _draft(_event(MOVE_REF))

    resolve_places(draft, _request())

    assert draft.events[0].place == "도착 공원"


def test_calendar_supplies_the_friendly_label_when_no_location_evidence():
    draft = _draft(_event(CALENDAR_REF))

    resolve_places(draft, _request())

    assert draft.events[0].place == "집"


# --- address ------------------------------------------------------------------


def test_missing_address_is_filled_from_the_stay_address():
    draft = _draft(_event(STAY_REF))

    resolve_places(draft, _request())

    assert draft.events[0].address == HOME_ADDRESS


def test_photo_only_event_gets_no_address():
    # 사진에는 좌표만 있다. 좌표로 주소를 만들지 않는다.
    draft = _draft(_event(PHOTO_REF))

    resolve_places(draft, _request())

    assert draft.events[0].address is None


def test_an_invented_address_is_removed_and_warned():
    draft = _draft(_event(PHOTO_REF, address="서울특별시 강남구 테헤란로 152", title="지어낸 주소"))

    resolve_places(draft, _request())

    assert draft.events[0].address is None
    warning = next(w for w in draft.warnings if "정확한 입력 근거가 없는 주소" in w.message)
    assert warning.severity is TimelineWarningSeverity.MEDIUM
    assert "지어낸 주소" in warning.message


@pytest.mark.parametrize(
    "text", ["경기도 오산시 운암로 90 인근", "강남역 부근", "학교 근처", "", None]
)
def test_approximate_text_is_not_an_exact_address(text):
    assert not is_exact_address(text)


def test_exact_address_passes():
    assert is_exact_address(HOME_ADDRESS)


def test_approximate_evidence_address_is_skipped_when_filling():
    # 수집 원본이 도착지 주소를 `... 90 인근` 으로 준다. 그것은 주소가 아니다.
    request = _request()
    request.movements[0].end = GeoPlace(place="도착 공원", address="경기도 오산시 운암로 90 인근")
    draft = _draft(_event(MOVE_REF))

    resolve_places(draft, request)

    # 도착지가 근사값이라 건너뛰고, 정확한 출발지 주소로 채운다.
    assert draft.events[0].address == "경기도 오산시 운암로 90"
    assert draft.events[0].place == "도착 공원"  # place 은 근사여도 이름이다


def test_llm_address_with_an_approximate_suffix_is_removed():
    draft = _draft(_event(STAY_REF, address=f"{HOME_ADDRESS} 인근", title="근사 주소"))

    resolve_places(draft, _request())

    # 지워진 뒤 근거의 정확한 주소로 다시 채워진다.
    assert draft.events[0].address == HOME_ADDRESS
    assert any("정확한 입력 근거가 없는 주소" in w.message for w in draft.warnings)


def test_an_address_supported_by_the_calendar_memo_survives():
    # locationText `집(경기도 오산시 운암로 90)` 이 그 주소를 품고 있다.
    draft = _draft(_event(CALENDAR_REF, address=HOME_ADDRESS))

    resolve_places(draft, _request())

    assert draft.events[0].address == HOME_ADDRESS
    assert draft.warnings == []


def test_a_supported_address_is_kept_verbatim():
    draft = _draft(_event(STAY_REF, address=HOME_ADDRESS))

    resolve_places(draft, _request())

    assert draft.events[0].address == HOME_ADDRESS
    assert draft.warnings == []


# --- 후보 장소 복사 (#72) -------------------------------------------------------
#
# candidate 의 place/places/address 는 Agent 가 아니라 코드가 입력에서 그대로 복사한다.
# 한 지점에 이름이 여럿일 때 어느 것이 사용자의 `회사` 인지는 User Memory 를 가진
# Timeline 만 판단할 수 있으므로, 후보를 줄이지 않고 places 로 전부 넘긴다.


def _candidate(*refs, places=None, address=None) -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.REST,
        time_range=CandidateTimeRange(
            start_time="2026-06-20T09:00:00+09:00",
            end_time="2026-06-20T10:00:00+09:00",
        ),
        title="후보",
        description="설명",
        places=places or [],
        address=address,
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=st, raw_id=fixture_raw_id(raw_id))
            for st, raw_id in refs
        ],
    )


def test_candidate_place_is_copied_from_the_stay():
    result = AgentEventResult(candidates=[_candidate(STAY_REF)])

    resolve_candidate_places(result, _request())

    candidate = result.candidates[0]
    assert candidate.places == [APARTMENT]
    assert candidate.address == HOME_ADDRESS


def test_candidate_keeps_every_place_name_of_the_referenced_input():
    # 한 지점에 이름이 여럿이면 줄이지 않는다. 고르는 것은 Timeline 의 일이다.
    request = _request()
    request.stays[0].places = ["강남파이낸스센터", APARTMENT]
    result = AgentEventResult(candidates=[_candidate(STAY_REF)])

    resolve_candidate_places(result, request)

    # 순서를 지켜 디듀프한다. 목록의 순서가 곧 근거 우선순위다.
    assert result.candidates[0].places == [APARTMENT, "강남파이낸스센터"]


def test_candidate_stay_place_wins_over_movement_and_calendar():
    result = AgentEventResult(candidates=[_candidate(CALENDAR_REF, MOVE_REF, STAY_REF)])

    resolve_candidate_places(result, _request())

    candidate = result.candidates[0]
    # STAY 가 우선이라 목록 맨 앞이고, 나머지 근거의 장소명도 뒤에 실린다.
    assert candidate.places[0] == APARTMENT
    assert "도착 공원" in candidate.places
    assert "집" in candidate.places


def test_candidate_movement_prefers_its_destination_place():
    result = AgentEventResult(candidates=[_candidate(MOVE_REF)])

    resolve_candidate_places(result, _request())

    assert result.candidates[0].places == ["도착 공원"]


def test_candidate_places_written_by_the_agent_are_replaced_by_the_input():
    result = AgentEventResult(candidates=[_candidate(STAY_REF, places=["지어낸 장소"])])

    resolve_candidate_places(result, _request())

    assert result.candidates[0].places == [APARTMENT]


def test_candidate_address_written_by_the_agent_is_dropped_without_evidence():
    # 사진에는 좌표뿐이라 주소를 만들어 낼 근거가 없다.
    result = AgentEventResult(
        candidates=[_candidate(PHOTO_REF, address="서울특별시 강남구 테헤란로 152")]
    )

    resolve_candidate_places(result, _request())

    assert result.candidates[0].address is None


def test_candidate_without_place_evidence_is_left_empty():
    result = AgentEventResult(candidates=[_candidate(PHOTO_REF, places=["배스킨라빈스"])])

    resolve_candidate_places(result, _request())

    # PHOTO 에는 아직 place 필드가 없다. 코드가 재현할 수 없는 값은 남기지 않는다.
    assert result.candidates[0].places == []


def test_candidate_evidence_is_found_even_when_the_agent_mislabels_source_type():
    # rawId 는 UUID 라 타입을 가로질러 유일하다. 라벨이 틀려도 입력을 찾는다.
    result = AgentEventResult(
        candidates=[_candidate((EventSourceType.PHOTO, "stay-1"))]
    )

    resolve_candidate_places(result, _request())

    assert result.candidates[0].places == [APARTMENT]


def test_candidate_approximate_address_is_not_used():
    request = _request()
    request.stays[0].address = f"{HOME_ADDRESS} 인근"
    result = AgentEventResult(candidates=[_candidate(STAY_REF)])

    resolve_candidate_places(result, request)

    assert result.candidates[0].address is None


def test_candidate_vague_place_names_are_not_copied():
    request = _request()
    request.stays[0].place = "한 곳"
    request.stays[0].places = ["근처"]
    result = AgentEventResult(candidates=[_candidate(STAY_REF)])

    resolve_candidate_places(result, request)

    assert result.candidates[0].places == []


# --- 보존 검사 (#72) ------------------------------------------------------------


def test_a_place_with_no_evidence_anywhere_is_warned_but_kept():
    draft = _draft(_event(PHOTO_REF, place="배스킨라빈스", title="사진 event"))

    resolve_places(draft, _request())

    # 지우지 않는다 — 코드가 재현할 수 없는 값이라 지우면 그것까지 잃는다.
    assert draft.events[0].place == "배스킨라빈스"
    warning = next(w for w in draft.warnings if "없는 장소명" in w.message)
    assert warning.severity is TimelineWarningSeverity.LOW


def test_a_place_backed_by_the_input_is_not_warned():
    draft = _draft(_event(STAY_REF, place=APARTMENT))

    resolve_places(draft, _request())

    assert not any("없는 장소명" in w.message for w in draft.warnings)


def test_a_home_label_from_user_memory_is_not_warned():
    # `집` 은 입력에 없고 User Memory 에만 있다. 그 라벨을 붙이게 하는 것이 #72 의 목적이다.
    request = _request()
    request.user_memory = UserMemory(
        life_context=f"평일에는 {APARTMENT}에 있는 집에서 지냅니다."
    )
    draft = _draft(_event(STAY_REF, place="집"))

    resolve_places(draft, request)

    assert draft.events[0].place == "집"
    assert not any("없는 장소명" in w.message for w in draft.warnings)


def test_place_warnings_are_remeasured_on_every_pass():
    draft = _draft(_event(PHOTO_REF, place="배스킨라빈스"))
    request = _request()

    resolve_places(draft, request)
    resolve_places(draft, request)

    # 반복마다 자기 이전 warning 을 지우고 다시 잰다.
    assert len([w for w in draft.warnings if "없는 장소명" in w.message]) == 1


def test_candidate_movement_takes_only_the_destination():
    """이동의 장소는 도착지 하나다. 출발지를 섞으면 방향이 뒤집힌다."""

    request = _request()
    request.movements[0].start = GeoPlace(place="집", address="경기도 오산시 운암로 90")
    request.movements[0].end = GeoPlace(place="한강공원", address="서울 영등포구 여의동로 330")
    result = AgentEventResult(candidates=[_candidate(MOVE_REF)])

    resolve_candidate_places(result, request)

    candidate = result.candidates[0]
    # `집` 이 섞이면 Timeline 이 User Memory 와 대조해 "집에 있었다" 로 뒤집어 읽는다.
    assert candidate.places == ["한강공원"]
    assert candidate.address == "서울 영등포구 여의동로 330"


def test_candidate_address_is_empty_rather_than_the_other_endpoint():
    """도착지 주소가 근사값이면 비운다. 출발지 주소로 넘어가면 place 와 짝이 어긋난다."""

    request = _request()
    request.movements[0].start = GeoPlace(place="집", address="경기도 오산시 운암로 90")
    request.movements[0].end = GeoPlace(place="한강공원", address="서울 영등포구 여의동로 330 인근")
    result = AgentEventResult(candidates=[_candidate(MOVE_REF)])

    resolve_candidate_places(result, request)

    candidate = result.candidates[0]
    assert candidate.places == ["한강공원"]
    assert candidate.address is None


# --- 이름 통일 back-compat (#72) ------------------------------------------------


def test_the_old_place_label_key_is_still_accepted():
    """동결 프롬프트나 롤백으로 모델이 옛 키를 내도 값이 사라지지 않아야 한다.

    `TimelineEventDraft` 는 모르는 키를 무시하므로, alias 가 없으면 장소가 조용히
    없어진다. 에러도 warning 도 남지 않아 추적이 안 되는 실패다.
    """

    event = TimelineEventDraft.model_validate(
        {
            "clientEventId": "event-001",
            "eventType": "REST",
            "title": "이벤트",
            "startTime": "2026-06-20T09:00:00+09:00",
            "endTime": "2026-06-20T10:00:00+09:00",
            "confidence": 0.7,
            "inferenceLevel": "EVIDENCE_BASED",
            "sourceRefs": [
                {"sourceType": "STAY", "rawId": fixture_raw_id("stay-1")}
            ],
            "placeLabel": "두꺼비 감자탕 지산점",
        }
    )

    assert event.place == "두꺼비 감자탕 지산점"


def test_the_draft_serializes_place_under_the_unified_name():
    event = _event(STAY_REF, place="집")

    body = event.model_dump(by_alias=True, mode="json")

    assert body["place"] == "집"
    assert "placeLabel" not in body
