"""v4 세트의 main agent 경로 (#167~#171).

Event Agent 다섯 대신 사진·알림 단서만 LLM 으로 만들고, 위치 뼈대·일정 단서·근거 묶음은
코드가 만든다. Timeline 은 묶음 id 로 근거를 가리키고, 확정 pass 가 장소 칸을 정한다.
그래프를 그대로 태운다 — 배선을 흉내 내면 배선이 깨져도 테스트가 통과한다.
"""

import asyncio
import json

import pytest

from app.agents.events.notification.clue_agent import NotificationClueAgent
from app.agents.events.photo.clue_agent import PhotoClueAgent
from app.agents.events.photo.describer import PhotoDescriber
from app.agents.main import main_agent
from app.agents.main import run_main_agent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.schemas import EventType, GeoPlace, MovementItem
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.pipeline import confirm_only_repair_agent, silent_question_agent
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    photo_item,
    stay_item,
)


class _NoDescribe(PhotoDescriber):
    def describe(self, photos):
        return {}


@pytest.fixture(autouse=True)
def clue_contract(monkeypatch):
    monkeypatch.setattr(main_agent, "uses_clue_contract", lambda: True)


def _day():
    home = stay_item(
        "home", lat=37.50, start="2026-06-20T06:00:00", end="2026-06-20T08:30:00",
        places=["한빛아파트"], address="서울 마포구 도화동 17-14",
    )
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"),
        start_at="2026-06-20T08:30:00",
        end_at="2026-06-20T09:10:00",
        start=GeoPlace(latitude=37.50, longitude=127.0, address="서울 마포구 도화동 17-14"),
        end=GeoPlace(latitude=37.60, longitude=127.0, address="서울 강남구 강남대로 396", places=["OO타워"]),
        transports=["IN_VEHICLE"],
        distance_meters=12000,
    )
    office = stay_item(
        "office", lat=37.60, start="2026-06-20T09:10:00", end="2026-06-20T18:00:00",
        places=["OO타워 주차장", "OO타워", "OO빌딩", "OO식당"], address="서울 강남구 강남대로 396",
    )
    lunch = photo_item("lunch", taken="2026-06-20T12:30:00", lat=37.6001, lon=127.0, places=["OO식당"])
    lunch.description = "순두부찌개와 만두가 놓인 식탁"
    meeting = calendar_item("m", "주간 회의", start="2026-06-20T10:00:00", end="2026-06-20T11:00:00")
    return make_request(stays=[home, office], movements=[commute], photos=[lunch], calendars=[meeting])


def _timeline_response() -> str:
    # 뼈대: B1/E1 집(06:00~08:30), B2/E2 이동, B3/E3 회사(일정 C1·사진 P1 이 붙음).
    return json.dumps(
        {
            "events": [
                {
                    "eventType": "UNKNOWN", "title": "도화동에서 보낸 아침",
                    "description": "{place}에서 시간을 보냈다.", "from": ["E1"],
                    "startTime": "2026-06-20T06:00:00+09:00", "endTime": "2026-06-20T08:30:00+09:00",
                    "confidence": 0.7, "inferenceLevel": "DIRECT", "mergeReason": "집 체류 하나",
                    "placeCandidates": ["한빛아파트"],
                },
                {
                    "eventType": "MOVEMENT", "title": "출근", "description": "", "from": ["E2"],
                    "startTime": "2026-06-20T08:30:00+09:00", "endTime": "2026-06-20T09:10:00+09:00",
                    "confidence": 0.9, "inferenceLevel": "DIRECT", "mergeReason": "이동 구간",
                },
                {
                    "eventType": "WORK", "title": "주간 회의와 업무",
                    "description": "{place}에서 주간 회의를 했다.", "from": ["E3"], "evidence": ["C1"],
                    "startTime": "2026-06-20T09:10:00+09:00", "endTime": "2026-06-20T18:00:00+09:00",
                    "confidence": 0.8, "inferenceLevel": "EVIDENCE_BASED",
                    "mergeReason": "회사 체류와 회의 일정", "placeCandidates": ["OO타워", "OO타워 주차장", "OO빌딩"],
                },
                {
                    "eventType": "MEAL", "title": "순두부찌개와 만두",
                    "description": "OO식당에서 순두부찌개와 만두를 먹었다.", "from": ["E3"], "evidence": ["P1"],
                    "startTime": "2026-06-20T12:10:00+09:00", "endTime": "2026-06-20T12:50:00+09:00",
                    "confidence": 0.85, "inferenceLevel": "DIRECT",
                    "mergeReason": "B3 안의 점심 사진(P1)을 따로 두었다", "placeCandidates": ["OO식당"],
                },
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def _run(request, timeline_response):
    timeline_llm = FakeLLM([timeline_response])
    photo_llm = FakeLLM([json.dumps({"photos": [{"id": "P1", "activity": "순두부찌개와 만두를 먹었다"}]}, ensure_ascii=False)])
    draft = asyncio.run(
        run_main_agent(
            request,
            timeline_agent=TimelineAgent(llm=timeline_llm),
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
            photo_clue_agent=PhotoClueAgent(llm=photo_llm, describer=_NoDescribe()),
            notification_clue_agent=NotificationClueAgent(llm=FakeLLM([json.dumps({"clues": []})])),
        )
    )
    return draft, timeline_llm


def test_v4_pipeline_bundles_clues_and_fills_place_slots():
    request = _day()

    draft, timeline_llm = _run(request, _timeline_response())

    prompt = timeline_llm.calls[0].prompt
    assert "[근거 묶음]" in prompt and '"id": "B3"' in prompt
    assert request.stays[0].raw_id not in prompt, "Timeline 입력에 rawId 를 싣지 않는다."

    by_type = {event.event_type: event for event in draft.events}
    movement = by_type[EventType.MOVEMENT]
    assert movement.description == "마포구 도화동에서 강남구 강남대로로 이동했다.", "이동 문장은 주소 원문이 아니라 지명(구 + 동)이다."
    assert movement.place is None

    work = by_type[EventType.WORK]
    assert work.description == "{place}에서 주간 회의를 했다."
    assert work.place_candidates == ["OO타워", "OO빌딩", "OO식당"], (
        "Timeline 이 고른 순서 뒤에 나머지 이름이 붙고, 주차장은 후보가 되지 않는다."
    )
    assert work.place == "OO타워"
    meeting_raw_id = request.calendars[0].raw_id
    assert meeting_raw_id in {ref.raw_id for ref in work.source_refs}

    meal = by_type[EventType.MEAL]
    assert meal.description == "{place}에서 순두부찌개와 만두를 먹었다."
    photo_raw_id = request.photos[0].raw_id
    assert photo_raw_id in {ref.raw_id for ref in meal.source_refs}
    assert photo_raw_id not in {ref.raw_id for ref in work.source_refs}, (
        "다른 event 가 단서 id 로 가져간 사진은 묶음을 펼칠 때 빠진다."
    )
    assert work.merge_reason == "회사 체류와 회의 일정"
    assert work.base_event_ids == ["E3"]
    assert meal.base_event_ids == ["E3"], "꺼낸 식사도 그 기본 event 에서 왔다."
    office_raw_id = request.stays[1].raw_id
    assert office_raw_id in {ref.raw_id for ref in work.source_refs}


def test_v4_pipeline_does_not_call_the_v3_event_agents():
    request = _day()

    class Exploding:
        name = "boom"

        def generate(self, request):
            raise AssertionError("v4 는 Event Agent 를 부르지 않는다")

    draft = asyncio.run(
        run_main_agent(
            request,
            event_agents=[Exploding()],
            timeline_agent=TimelineAgent(llm=FakeLLM([_timeline_response()])),
            repair_agent=confirm_only_repair_agent(),
            question_agent=silent_question_agent(),
            photo_clue_agent=PhotoClueAgent(llm=FakeLLM([json.dumps({"photos": []})]), describer=_NoDescribe()),
            notification_clue_agent=NotificationClueAgent(llm=FakeLLM([json.dumps({"clues": []})])),
        )
    )

    assert draft.events


def _clues_with_lunch_photo(request):
    from datetime import datetime, timedelta, timezone

    from app.schemas.clues import PhotoClue
    from app.services.calendar_clues import build_calendar_clues
    from app.services.clue_bundles import build_timeline_clues
    from app.services.location_skeleton import build_location_skeleton

    photo = request.photos[0]
    return build_timeline_clues(
        build_location_skeleton(request),
        build_calendar_clues(request),
        [
            PhotoClue(
                clue_id="P1",
                raw_id=photo.raw_id,
                taken_at=datetime(2026, 6, 20, 12, 30, tzinfo=timezone(timedelta(hours=9))),
                latitude=photo.latitude,
                longitude=photo.longitude,
            )
        ],
        [],
    )


def _one_event(event_type, title, from_, evidence, at):
    return json.dumps(
        {
            "events": [
                {
                    "eventType": event_type, "title": title, "description": f"{title}.",
                    "from": from_, "evidence": evidence,
                    "startTime": f"2026-06-20T{at}:00+09:00", "endTime": f"2026-06-20T{at}:00+09:00",
                    "confidence": 0.7, "inferenceLevel": "DIRECT", "mergeReason": "근거",
                },
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def test_lineage_to_a_base_event_at_another_time_is_ignored():
    """오전 event 에 저녁 체류의 기본 event 를 적은 실제 출력. 그 체류 기록을 근거로 붙이지 않는다."""

    from app.agents.timeline.timeline_agent import parse_timeline_clue_draft

    request = _day()
    text = _one_event("UNKNOWN", "아침", ["E3"], ["B1"], "07:00")

    [event] = parse_timeline_clue_draft(text, request, _clues_with_lunch_photo(request)).events

    assert event.base_event_ids == []
    assert request.stays[1].raw_id not in {r.raw_id for r in event.source_refs}


def test_evidence_taken_at_another_time_is_not_an_events_evidence():
    """12:30 점심 사진을 07:00 event 의 근거로 적으면 뺀다 — 근거는 시간과 장소가 모두 맞아야 한다
    (사용자 결정 2026-10-30·31). 같은 곳에 두 번 간 날 점심 사진이 아침 체류의 근거로 적혔고, Repair 가
    그 말을 믿고 아침 체류를 식사로 바꿨다. 근거가 남지 않으면 event 를 만들지 않고 알린다(사진은 확정
    pass 가 촬영 시각으로 되살린다).
    """

    from app.agents.timeline.timeline_agent import parse_timeline_clue_draft

    request = _day()
    draft = parse_timeline_clue_draft(
        _one_event("WORK", "보수 작업", [], ["P1"], "07:00"), request, _clues_with_lunch_photo(request)
    )

    assert draft.events == []
    assert any("P1" in w.message and "맞지 않는" in w.message for w in draft.warnings)

    kept = parse_timeline_clue_draft(
        _one_event("MEAL", "점심", ["E3"], ["P1"], "12:30"), request, _clues_with_lunch_photo(request)
    )
    assert request.photos[0].raw_id in {r.raw_id for r in kept.events[0].source_refs}, "시간·장소가 맞으면 둔다."


def test_a_photo_event_without_lineage_gets_the_segment_its_photo_is_in():
    """`from` 을 비운 식사 event 는 그 사진이 붙은 위치 구간에서 온 것으로 본다(사용자 결정 2026-10-31).

    사진 입력에서 장소를 뺀 뒤 Timeline 이 꺼낸 식사의 `from` 을 비우자, 그 식사가 장소도 지명도 없는
    `점심을 먹었다` 가 됐다. 구간의 체류 기록이 근거로 붙어야 장소 후보와 지명이 생긴다.
    """

    from app.agents.timeline.timeline_agent import parse_timeline_clue_draft

    request = _day()
    draft = parse_timeline_clue_draft(
        _one_event("MEAL", "점심", [], ["P1"], "12:30"), request, _clues_with_lunch_photo(request)
    )

    [meal] = draft.events
    assert meal.base_event_ids == ["E3"]
    assert request.stays[1].raw_id in {r.raw_id for r in meal.source_refs}
