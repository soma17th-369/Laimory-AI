"""Repair 를 지난 draft 가 지켜야 하는 것 (#119).

**사진은 예외 없이 정확히 한 event 에만 있다.** Repair 가 무엇을 하든, 실패하든, 아예 돌지
않든 같다. 제한 시간이 끝나면 마지막으로 발행된 확정본이 저장되므로(#76) 반환된 draft
뿐 아니라 **발행된 확정본 전부**가 이 상태여야 한다.

**이동 사이의 장시간 체류는 Repair 가 나눈다.** 코드는 찾아서 넘기고 나누지 않는다. 그래서
Repair 가 돌지 않으면 위반은 남고, 그 사실이 warning 으로 드러나야 한다.
"""

import asyncio
import json

import pytest

from app.agents.main import run_main_agent
from app.agents.repair import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarningSeverity,
)
from app.services.photo_guard import inspect_photo_assignment
from app.services.timeline_validator import validate_timeline_for_storage
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.pipeline import (
    StubEventAgent,
    confirm_only_repair_agent,
    silent_question_agent,
)
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    movement_item,
    photo_item,
    stay_item,
)

DAY = "2026-06-20"

OUT = fixture_raw_id("move-out")
BACK = fixture_raw_id("move-back")
OFFICE = fixture_raw_id("stay-office")
CAFE = fixture_raw_id("stay-cafe")
LUNCH_PHOTO = fixture_raw_id("photo-1")
EVENING_PHOTO = fixture_raw_id("photo-2")


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _request():
    return make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=f"{DAY}T08:20:00", end=f"{DAY}T09:00:00"),
            movement_item(2, raw_id="move-back", start=f"{DAY}T18:00:00", end=f"{DAY}T19:00:00"),
        ],
        stays=[
            stay_item(1, raw_id="stay-office", start=f"{DAY}T09:00:00", end=f"{DAY}T18:00:00", place="회사", places=["회사"]),
            stay_item(2, raw_id="stay-cafe", start=f"{DAY}T19:30:00", end=f"{DAY}T21:00:00", lat=37.6, lon=127.2, place="카페", places=["카페"]),
        ],
        photos=[
            photo_item("1", taken=f"{DAY}T12:30:00"),
            photo_item("2", taken=f"{DAY}T20:00:00"),
        ],
    )


def _event(
    client_event_id: str,
    start: str,
    end: str,
    *refs: tuple[EventSourceType, str],
    event_type=EventType.WORK,
    title: str | None = None,
) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title or client_event_id,
        description="설명",
        start_time=_t(start),
        end_time=_t(end),
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=source_type, raw_id=raw_id)
            for source_type, raw_id in refs
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )


def _plan(tool_calls: list[dict], *, done: bool = True) -> str:
    return json.dumps(
        {"issues": [], "toolCalls": tool_calls, "done": done, "summary": "요약"},
        ensure_ascii=False,
    )


STAY = EventSourceType.STAY
PHOTO = EventSourceType.PHOTO
MOVEMENT = EventSourceType.MOVEMENT


def _morning():
    return _event("event-001", "09:00", "12:00", (STAY, OFFICE), title="오전 근무")


def _lunch(*extra):
    return _event(
        "event-002", "12:20", "12:50", (STAY, OFFICE), *extra,
        event_type=EventType.MEAL, title="점심",
    )


def _evening(*extra):
    return _event(
        "event-003", "19:30", "21:00", (STAY, CAFE), *extra,
        event_type=EventType.REST, title="카페",
    )


def assert_photo_contract(draft: TimelineDraft, request) -> None:
    assignment = inspect_photo_assignment(draft, request)
    assert assignment.missing == set(), "어느 event 에도 없는 사진이 있습니다."
    assert assignment.duplicated == {}, "여러 event 에 걸린 사진이 있습니다."
    valid = {item.raw_id for item in request.iter_source_items()}
    assert validate_timeline_for_storage(draft, valid) == []


def _run(draft: TimelineDraft, llm_responses: list, **kwargs):
    """Repair 를 돌리고 반환된 draft 와 발행된 확정본을 함께 돌려준다."""

    published: list[TimelineDraft] = []
    max_iterations = kwargs.pop("max_iterations", 3)
    # `split_event` 는 v3 세트의 도구다. 세트와 무관하게 확인하려고 직접 켠다.
    result = RepairAgent(
        llm=FakeLLM(llm_responses), max_iterations=max_iterations, extended_input=True
    ).generate(_request(), draft, on_confirm=published.append, **kwargs)
    return result, published


# --- 사진 단일 귀속 --------------------------------------------------------------

_DUPLICATED = "duplicated"
_MISSING = "missing"
_MISLABELLED = "mislabelled"
_PHOTO_CARD_DUPLICATE = "photo-card-duplicate"


def _broken_draft(kind: str) -> TimelineDraft:
    if kind == _DUPLICATED:
        return _draft(
            _morning(),
            _lunch((PHOTO, LUNCH_PHOTO)),
            _evening((PHOTO, LUNCH_PHOTO), (PHOTO, EVENING_PHOTO)),
        )
    if kind == _MISSING:
        return _draft(_morning(), _lunch(), _evening())
    if kind == _MISLABELLED:
        # LLM 이 사진 참조에 `STAY` 라고 적었다.
        return _draft(
            _morning(),
            _lunch((STAY, LUNCH_PHOTO)),
            _evening((STAY, LUNCH_PHOTO), (PHOTO, EVENING_PHOTO)),
        )
    return _draft(
        _morning(),
        _lunch((PHOTO, LUNCH_PHOTO)),
        _event("event-004", "12:30", "12:30", (PHOTO, LUNCH_PHOTO), event_type=EventType.PHOTO_MOMENT, title="사진"),
        _evening((PHOTO, EVENING_PHOTO)),
    )


_BROKEN = [_DUPLICATED, _MISSING, _MISLABELLED, _PHOTO_CARD_DUPLICATE]


@pytest.mark.parametrize("kind", _BROKEN)
def test_photo_contract_holds_without_any_llm_iteration(kind):
    """LLM 이 꺼져 있어도 코드 확정만으로 지켜진다."""

    result, published = _run(_broken_draft(kind), [_plan([])], max_iterations=0)

    assert_photo_contract(result, _request())
    for draft in published:
        assert_photo_contract(draft, _request())


@pytest.mark.parametrize("kind", _BROKEN)
def test_photo_contract_holds_when_the_llm_fails(kind):
    result, published = _run(_broken_draft(kind), [RuntimeError("LLM 장애")])

    assert_photo_contract(result, _request())
    for draft in published:
        assert_photo_contract(draft, _request())


@pytest.mark.parametrize("kind", _BROKEN)
def test_photo_contract_holds_when_the_response_is_unreadable(kind):
    result, _ = _run(_broken_draft(kind), ["JSON 이 아닌 응답"])

    assert_photo_contract(result, _request())


def test_lunch_keeps_its_photo_and_the_photo_card_is_removed():
    result, _ = _run(_broken_draft(_PHOTO_CARD_DUPLICATE), [_plan([])], max_iterations=0)

    assert [event.title for event in result.events] == ["오전 근무", "점심", "카페"]
    lunch = result.events[1]
    assert LUNCH_PHOTO in [ref.raw_id for ref in lunch.source_refs]


def test_photo_contract_holds_after_repair_deletes_the_event_holding_a_photo():
    draft = _draft(_morning(), _lunch((PHOTO, LUNCH_PHOTO)), _evening((PHOTO, EVENING_PHOTO)))
    plan = _plan([{"tool": "delete_event", "args": {"clientEventId": "event-002"}}])

    result, published = _run(draft, [plan])

    assert [event.title for event in result.events] == ["오전 근무", "카페"]
    assert_photo_contract(result, _request())
    for confirmed in published:
        assert_photo_contract(confirmed, _request())


def test_photo_contract_holds_after_repair_copies_a_photo_into_another_event():
    draft = _draft(_morning(), _lunch((PHOTO, LUNCH_PHOTO)), _evening((PHOTO, EVENING_PHOTO)))
    plan = _plan(
        [
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-001",
                    "fields": {
                        "sourceRefs": [
                            {"sourceType": "STAY", "rawId": OFFICE},
                            {"sourceType": "PHOTO", "rawId": LUNCH_PHOTO},
                        ]
                    },
                },
            }
        ]
    )

    result, _ = _run(draft, [plan])

    assert_photo_contract(result, _request())
    # 촬영 시각을 포함하는 쪽이 가진다. 한쪽에만 넣으면 코드가 되돌린다.
    assert LUNCH_PHOTO in [ref.raw_id for ref in result.events[1].source_refs]


def test_repair_moves_a_photo_by_changing_both_events_in_one_plan():
    draft = _draft(_morning(), _lunch((PHOTO, LUNCH_PHOTO)), _evening((PHOTO, EVENING_PHOTO)))
    plan = _plan(
        [
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-002",
                    "fields": {"sourceRefs": [{"sourceType": "STAY", "rawId": OFFICE}]},
                },
            },
            {
                "tool": "update_event",
                "args": {
                    "clientEventId": "event-001",
                    "fields": {
                        "sourceRefs": [
                            {"sourceType": "STAY", "rawId": OFFICE},
                            {"sourceType": "PHOTO", "rawId": LUNCH_PHOTO},
                        ]
                    },
                },
            },
        ]
    )

    result, _ = _run(draft, [plan])

    assert_photo_contract(result, _request())
    assert LUNCH_PHOTO in [ref.raw_id for ref in result.events[0].source_refs]


def _candidate(title: str, start: str, end: str, raw_id: str) -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.WORK,
        time_range=CandidateTimeRange(start_time=_t(start), end_time=_t(end)),
        title=title,
        description="설명",
        source_refs=[SourceRef(source_type=STAY, raw_id=raw_id)],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
    )


def _timeline_response_without_photos() -> str:
    """사진을 한 장도 넣지 않은 Timeline 응답."""

    return json.dumps(
        {
            "events": [
                {
                    "eventType": "WORK",
                    "title": "다시 만든 근무",
                    "description": "회사에서 일했어요.",
                    "startTime": _t("09:00"),
                    "endTime": _t("12:00"),
                    "confidence": 0.8,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [{"sourceType": "STAY", "rawId": OFFICE}],
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def test_photo_contract_holds_after_timeline_is_rerun_and_drops_the_photos():
    draft = _draft(_morning(), _lunch((PHOTO, LUNCH_PHOTO)), _evening((PHOTO, EVENING_PHOTO)))
    plan = _plan([{"tool": "rerun_timeline_agent", "args": {}}])

    result, published = _run(
        draft,
        [plan],
        event_results={
            "location": AgentEventResult(
                candidates=[_candidate("회사", "09:00", "12:00", OFFICE)]
            )
        },
        timeline_agent=TimelineAgent(llm=FakeLLM([_timeline_response_without_photos()])),
    )

    assert [event.title for event in result.events] == ["다시 만든 근무"]
    assert_photo_contract(result, _request())
    for confirmed in published:
        assert_photo_contract(confirmed, _request())


def test_photo_is_kept_when_the_day_has_no_event_at_all():
    result, published = _run(_draft(), [_plan([])], max_iterations=0)

    assert [event.event_type for event in result.events] == [EventType.PHOTO_MOMENT] * 2
    assert [event.client_event_id for event in result.events] == ["event-001", "event-002"]
    assert_photo_contract(result, _request())
    for confirmed in published:
        assert_photo_contract(confirmed, _request())


def test_main_agent_returns_a_draft_that_keeps_the_photo_contract():
    """저장으로 넘어가는 draft 는 main agent 가 돌려준 것이다."""

    result = asyncio.run(
        run_main_agent(
            _request(),
            event_agents=[
                StubEventAgent(
                    AgentEventResult(candidates=[_candidate("회사", "09:00", "12:00", OFFICE)])
                )
            ],
            timeline_agent=TimelineAgent(llm=FakeLLM([_timeline_response_without_photos()])),
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
        )
    )

    assert_photo_contract(result, _request())


# --- 이동 사이 장시간 체류 --------------------------------------------------------


def _journey() -> TimelineDraft:
    """집을 나서 회사에 9시간 있다가 돌아온 하루가 이동 하나로 뭉개졌다."""

    return _draft(
        _event(
            "event-001", "08:20", "19:00",
            (MOVEMENT, OUT), (STAY, OFFICE), (MOVEMENT, BACK),
            event_type=EventType.MOVEMENT, title="회사에 다녀옴",
        ),
        _evening((PHOTO, LUNCH_PHOTO), (PHOTO, EVENING_PHOTO)),
    )


_SPLIT_JOURNEY = _plan(
    [
        {
            "tool": "split_event",
            "args": {
                "clientEventId": "event-001",
                "parts": [
                    {"startTime": _t("08:20"), "endTime": _t("09:00"), "title": "회사로 출근"},
                    {
                        "startTime": _t("09:00"),
                        "endTime": _t("18:00"),
                        "title": "회사에서 근무",
                        "eventType": "WORK",
                    },
                    {"startTime": _t("18:00"), "endTime": _t("19:00"), "title": "집으로 귀가"},
                ],
            },
        }
    ]
)


def _movement_stay_warnings(draft: TimelineDraft) -> list:
    return [
        warning
        for warning in draft.warnings
        if warning.warning_id.startswith("warning-movement-stay-")
    ]


def test_repair_splits_a_journey_that_swallowed_an_hours_long_stay():
    """두 이동 사이에 몇 시간 머문 입력이 하나의 긴 이동으로 남지 않는다."""

    result, _ = _run(_journey(), [_SPLIT_JOURNEY])

    assert [(event.event_type, event.title) for event in result.events[:3]] == [
        (EventType.MOVEMENT, "회사로 출근"),
        (EventType.WORK, "회사에서 근무"),
        (EventType.MOVEMENT, "집으로 귀가"),
    ]
    assert [
        [ref.raw_id for ref in event.source_refs] for event in result.events[:3]
    ] == [[OUT], [OFFICE], [BACK]]
    # 나눈 뒤 같은 구조 검사를 다시 지난다.
    assert _movement_stay_warnings(result) == []


def test_split_pieces_survive_the_confirm_pass():
    """확정의 병합이 나눈 조각을 다시 합치지 않는다."""

    result, _ = _run(_journey(), [_SPLIT_JOURNEY])

    times = [(event.start_time.isoformat(), event.end_time.isoformat()) for event in result.events[:3]]
    assert times == [
        (_t("08:20"), _t("09:00")),
        (_t("09:00"), _t("18:00")),
        (_t("18:00"), _t("19:00")),
    ]
    assert result.events[1].place == "회사"


def test_shrinking_the_time_alone_does_not_resolve_it():
    """시간만 줄이면 이동 근거가 남아 확정이 시간을 다시 늘린다. 근거가 나뉘어야 한다."""

    shrink = _plan(
        [
            {
                "tool": "update_event",
                "args": {"clientEventId": "event-001", "fields": {"endTime": _t("09:00")}},
            }
        ]
    )

    result, _ = _run(_journey(), [shrink])

    assert result.events[0].end_time.isoformat() == _t("19:00")
    assert len(_movement_stay_warnings(result)) == 1


@pytest.mark.parametrize(
    "responses",
    [
        pytest.param([_plan([])], id="repair-does-nothing"),
        pytest.param([RuntimeError("LLM 장애")], id="llm-fails"),
    ],
)
def test_unresolved_violation_is_left_visible(responses):
    """코드는 나누지 않는다. Repair 가 고치지 못하면 위반은 남고 warning 이 그것을 말한다."""

    result, _ = _run(_journey(), responses)

    assert result.events[0].title == "회사에 다녀옴"
    (warning,) = _movement_stay_warnings(result)
    assert warning.severity is TimelineWarningSeverity.HIGH


def test_unresolved_violation_is_left_visible_without_any_iteration():
    result, _ = _run(_journey(), [_plan([])], max_iterations=0)

    assert len(_movement_stay_warnings(result)) == 1


def test_journey_with_only_a_short_stay_is_kept_as_one():
    """짧은 센서 분절을 낀 정상적인 연속 이동은 그대로 둔다."""

    request = make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=f"{DAY}T08:20:00", end=f"{DAY}T09:00:00"),
            movement_item(2, raw_id="move-back", start=f"{DAY}T09:15:00", end=f"{DAY}T10:00:00"),
        ],
        stays=[
            stay_item(1, raw_id="stay-office", start=f"{DAY}T09:00:00", end=f"{DAY}T09:15:00", place="환승역")
        ],
    )
    draft = _draft(
        _event(
            "event-001", "08:20", "10:00",
            (MOVEMENT, OUT), (STAY, OFFICE), (MOVEMENT, BACK),
            event_type=EventType.MOVEMENT, title="회사로 출근",
        )
    )

    result = RepairAgent(max_iterations=0).generate(request, draft)

    assert [event.title for event in result.events] == ["회사로 출근"]
    assert _movement_stay_warnings(result) == []
