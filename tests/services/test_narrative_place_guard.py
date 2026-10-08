"""사용자 문장의 주소 원문·체류·시각 정리 (#138).

입력 주소 원문은 바꿀 값(장소명, 동·도로명)이 규칙으로 정해져 코드가 바꾼다. `체류` 제목과
제목의 시각은 무엇으로 바꿀지가 의미 판단이라 찾아서 Repair 에 넘긴다.
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
from app.services.confirm_report import ConfirmReport
from app.services.draft_repair import repair_draft
from app.services.narrative_place_guard import (
    find_narrative_labels,
    replace_input_addresses,
)
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

DAY = "2026-06-20"
ADDRESS = "서울특별시 예시구 예시로 123-4"


def _event(title: str, description: str = "", place: str | None = None) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id="event-001",
        event_type=EventType.UNKNOWN,
        title=title,
        description=description,
        start_time=f"{DAY}T13:00:00+09:00",
        end_time=f"{DAY}T14:00:00+09:00",
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        place=place,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=fixture_raw_id("stay-1"))
        ],
    )


def _draft(event: TimelineEventDraft) -> TimelineDraft:
    return TimelineDraft(user_id="u", date=DAY, timezone="Asia/Seoul", events=[event])


def _request(address: str = ADDRESS, place: str | None = None):
    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start=f"{DAY}T13:00:00",
                end=f"{DAY}T14:00:00",
                place=place,
                places=[place] if place else [],
                address=address,
            )
        ]
    )


# --- 입력 주소 원문 ---------------------------------------------------------------


def test_an_input_address_in_the_title_becomes_the_place_name() -> None:
    draft = _draft(
        _event(f"{ADDRESS}에서 보낸 시간", f"{ADDRESS}에서 쉬었어요.", place="카페 한빛")
    )

    replace_input_addresses(draft, _request())

    assert draft.events[0].title == "카페 한빛에서 보낸 시간"
    assert draft.events[0].description == "카페 한빛에서 쉬었어요."


def test_without_a_place_the_address_becomes_its_district() -> None:
    draft = _draft(_event(f"{ADDRESS}에서 보낸 시간"))

    replace_input_addresses(draft, _request())

    assert draft.events[0].title == "예시로에서 보낸 시간"


def test_an_approximate_input_address_is_found_without_its_suffix() -> None:
    draft = _draft(_event(f"{ADDRESS}에서 보낸 시간"))

    replace_input_addresses(draft, _request(address=f"{ADDRESS} 인근"))

    assert draft.events[0].title == "예시로에서 보낸 시간"


def test_the_road_part_of_an_input_address_is_replaced_too() -> None:
    """live 에서 모델이 시·도를 떼고 `예시로 123-4에서 보낸 밤` 으로 옮겼다."""

    draft = _draft(_event("예시로 123-4에서 보낸 밤"))

    replace_input_addresses(draft, _request())

    assert draft.events[0].title == "예시로에서 보낸 밤"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("서울특별시 관악구 지범로 12-3", "지범로에서 보낸 시간"),
        ("경기도 오산시 운암동 123", "운암동에서 보낸 시간"),
        ("서울특별시 예시구 중앙로12번길 34", "중앙로12번길에서 보낸 시간"),
        # 아파트의 `101동` 은 건물이다. 동네 이름으로 쓰지 않는다.
        ("경기도 오산시 운암동 한빛아파트 101동 1203호", "운암동에서 보낸 시간"),
    ],
)
def test_without_a_place_the_address_goes_down_to_the_dong_or_road(
    address: str, expected: str
) -> None:
    """시·군·구는 너무 넓다. 동이나 도로명까지 내려가고 건물번호·지번은 뺀다(사용자 결정)."""

    draft = _draft(_event(f"{address}에서 보낸 시간"))

    replace_input_addresses(draft, _request(address=address))

    assert draft.events[0].title == expected


def test_an_address_without_a_dong_or_road_is_left_for_repair() -> None:
    """시·군·구로 올라가지 않는다. 바꿀 이름이 없으면 그대로 두고 문장 검사가 짚는다."""

    draft = _draft(_event("서울특별시 예시구 123에서 보낸 시간"))

    replace_input_addresses(draft, _request(address="서울특별시 예시구 123"))

    assert draft.events[0].title == "서울특별시 예시구 123에서 보낸 시간"


def test_numbers_in_a_place_name_are_kept() -> None:
    """입력 주소와 글자 그대로 같은 부분만 바꾼다. 숫자를 일괄로 지우지 않는다."""

    draft = _draft(_event("2호선 강남역에서 환승", place="강남역"))

    replace_input_addresses(draft, _request())

    assert draft.events[0].title == "2호선 강남역에서 환승"


# --- 찾기 -----------------------------------------------------------------------


def _kinds(title: str, description: str = "") -> set[str]:
    return {item.kind for item in find_narrative_labels(_draft(_event(title, description)))}


@pytest.mark.parametrize("title", ["카페 체류", "마포 도화동 장기 체류", "회사 재체류"])
def test_a_stay_word_in_the_title_is_reported(title: str) -> None:
    assert "STAY_WORD_IN_TITLE" in _kinds(title)


@pytest.mark.parametrize("title", ["자정 전 귀가", "23시 카페", "새벽 2시 귀가", "11:30 점심"])
def test_a_clock_time_in_the_title_is_reported(title: str) -> None:
    assert "TIME_IN_TITLE" in _kinds(title)


@pytest.mark.parametrize("title", ["집에서 보낸 밤", "회사에서 오전 근무", "카페에서 보낸 시간"])
def test_a_part_of_the_day_is_allowed(title: str) -> None:
    assert _kinds(title) == set()


@pytest.mark.parametrize(
    "description",
    ["역삼동 123-4에서 쉬었어요.", "예시로 45에 들렀어요.", "101동 1203호에서 잤어요."],
)
def test_an_address_shape_in_the_narration_is_reported(description: str) -> None:
    assert "ADDRESS_IN_NARRATION" in _kinds("휴식", description)


@pytest.mark.parametrize(
    "description",
    ["2호선을 타고 이동했어요.", "건물 3층 카페에서 쉬었어요.", "역삼동에서 친구 2명을 만났어요."],
)
def test_numbers_that_are_not_an_address_are_not_reported(description: str) -> None:
    assert "ADDRESS_IN_NARRATION" not in _kinds("휴식", description)


# --- 확정 단계 --------------------------------------------------------------------


def test_v3_confirm_replaces_the_address_and_reports_the_stay_title() -> None:
    report = ConfirmReport()
    draft = _draft(_event(f"{ADDRESS} 체류"))

    repair_draft(draft, _request(), report=report, extended=True)

    assert ADDRESS not in draft.events[0].title
    assert {item["kind"] for item in report.findings} >= {"STAY_WORD_IN_TITLE"}


def test_v2_confirm_leaves_the_narration_alone() -> None:
    report = ConfirmReport()
    draft = _draft(_event(f"{ADDRESS} 체류"))

    repair_draft(draft, _request(), report=report, extended=False)

    assert draft.events[0].title == f"{ADDRESS} 체류"
    assert "STAY_WORD_IN_TITLE" not in {item["kind"] for item in report.findings}
