"""의미 보존과 안전한 병합 (#67).

실제 실행 로그에서 서로 다른 상대와 나눈 두 `SOCIAL` event 가 `같은 eventType +
빈 장소 + 시간 겹침` 만으로 합쳐졌다. 장소를 모른다는 것은 같은 곳이라는 뜻이 아니라
판단할 근거가 없다는 뜻이다.

장소 우선순위와 길이 상한도 함께 본다. 셋 다 "사용자가 읽는 결과에 사실이 남는가"라는
같은 질문의 다른 면이다.
"""

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    UserMemory,
)
from app.services.draft_repair import resolve_overlaps
from app.services.narrative_guard import (
    DESCRIPTION_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    enforce_narrative_length,
    shorten,
    verify_narrative_length,
)
from app.services.place_resolver import living_place_map, resolve_places
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

DAY = "2026-06-20"


def _event(
    client_event_id: str,
    *refs,
    event_type=EventType.SOCIAL,
    start="09:00",
    end="10:00",
    title=None,
    place_label=None,
    address=None,
    description="d",
) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title or client_event_id,
        description=description,
        place_label=place_label,
        address=address,
        start_time=f"{DAY}T{start}:00+09:00",
        end_time=f"{DAY}T{end}:00+09:00",
        confidence=0.5,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=source_type, raw_id=fixture_raw_id(raw_id))
            for source_type, raw_id in (refs or ((EventSourceType.STAY, "stay-1"),))
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="user-1234",
        date=DAY,
        timezone="Asia/Seoul",
        events=list(events),
        questions=[],
        warnings=[],
    )


# --- 병합 --------------------------------------------------------------------


def test_different_people_with_no_place_are_not_merged() -> None:
    """장소를 둘 다 모르고 근거도 다르면 별개의 사건이다."""

    draft = _draft(
        _event("민수와 정산", (EventSourceType.NOTIFICATION, "notif-a"), start="09:00", end="10:00"),
        _event("지현과 약속", (EventSourceType.NOTIFICATION, "notif-b"), start="09:30", end="10:30"),
    )

    resolve_overlaps(draft)

    assert [event.title for event in draft.events] == ["민수와 정산", "지현과 약속"]


def test_events_sharing_a_raw_id_are_still_merged() -> None:
    """같은 근거 원본을 가리키면 같은 사건이라는 가장 강한 신호다."""

    draft = _draft(
        _event("연락", (EventSourceType.NOTIFICATION, "notif-a"), start="09:00", end="10:00"),
        _event("같은 연락", (EventSourceType.NOTIFICATION, "notif-a"), start="09:30", end="10:30"),
    )

    resolve_overlaps(draft)

    assert len(draft.events) == 1


def test_same_place_events_are_still_merged() -> None:
    draft = _draft(
        _event("카페", (EventSourceType.STAY, "stay-a"), place_label="공덕 카페", start="09:00", end="10:00"),
        _event("카페 재방문", (EventSourceType.STAY, "stay-b"), place_label="공덕 카페", start="09:30", end="10:30"),
    )

    resolve_overlaps(draft)

    assert len(draft.events) == 1


# --- 장소 우선순위 -----------------------------------------------------------


def test_user_memory_living_place_wins_over_the_raw_place_name() -> None:
    """`오산운암3단지 주공아파트` 보다 사용자가 부르는 `집` 이 낫다."""

    request = make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start=f"{DAY}T09:00:00",
                end=f"{DAY}T10:00:00",
                place="오산운암3단지 주공아파트",
                places=[],
            )
        ],
        user_memory=UserMemory.model_validate(
            {"places": {"homePlace": "오산운암3단지 주공아파트"}}
        ),
    )
    draft = _draft(_event("아침", (EventSourceType.STAY, "stay-1")))

    resolve_places(draft, request)

    assert draft.events[0].place_label == "집"


def test_address_is_the_last_resort_label() -> None:
    """이름은 몰라도 주소는 아는 event 를 장소 없는 event 로 두지 않는다."""

    request = make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                start=f"{DAY}T09:00:00",
                end=f"{DAY}T10:00:00",
                place=None,
                address="경기도 오산시 운암로 90",
                places=[],
            )
        ]
    )
    draft = _draft(_event("오전", (EventSourceType.STAY, "stay-1")))

    resolve_places(draft, request)

    assert draft.events[0].place_label == "경기도 오산시 운암로 90"


def test_no_evidence_means_no_invented_place() -> None:
    request = make_request()
    draft = _draft(_event("연락", (EventSourceType.NOTIFICATION, "notif-a")))

    resolve_places(draft, request)

    assert draft.events[0].place_label is None
    assert draft.events[0].address is None


def test_living_place_map_is_empty_without_user_memory() -> None:
    assert living_place_map(None) == {}


# --- 길이 -------------------------------------------------------------------


def test_verify_warns_but_does_not_cut() -> None:
    """Repair 가 자연스럽게 다시 쓸 기회를 먼저 준다."""

    long_description = "가" * (DESCRIPTION_MAX_LENGTH + 10)
    draft = _draft(_event("긴 문장", description=long_description))

    verify_narrative_length(draft)

    assert draft.events[0].description == long_description
    assert len(draft.warnings) == 1


def test_enforce_cuts_only_what_repair_left_over() -> None:
    draft = _draft(
        _event(
            "긴 문장",
            title="가" * (TITLE_MAX_LENGTH + 5),
            description="나" * (DESCRIPTION_MAX_LENGTH + 5),
        )
    )

    enforce_narrative_length(draft)

    assert len(draft.events[0].title) <= TITLE_MAX_LENGTH
    assert len(draft.events[0].description) <= DESCRIPTION_MAX_LENGTH


def test_enforce_is_a_no_op_when_everything_fits() -> None:
    draft = _draft(_event("짧음", title="아침", description="아침을 먹었어요."))

    enforce_narrative_length(draft)

    assert draft.warnings == []


def test_repeated_enforce_does_not_keep_shrinking() -> None:
    draft = _draft(_event("긴 문장", description="나" * (DESCRIPTION_MAX_LENGTH + 5)))

    enforce_narrative_length(draft)
    once = draft.events[0].description
    enforce_narrative_length(draft)

    assert draft.events[0].description == once


def test_shorten_prefers_a_sentence_boundary() -> None:
    assert shorten("아침을 먹었어요. 그리고 걸었어요.", 15) == "아침을 먹었어요."


def test_shorten_falls_back_to_a_word_boundary() -> None:
    """문장 경계가 없으면 어절에서 끊는다. 글자 중간에서 끊는 것보다 낫다."""

    assert shorten("아주 긴 한 문장 이어짐 계속", 10) == "아주 긴 한 문장"


def test_shorten_falls_back_to_a_hard_cut() -> None:
    """끊을 곳이 없으면 하드 절단이 불가피하다. event 를 버리지는 않는다."""

    assert shorten("가" * 50, 10) == "가" * 10
