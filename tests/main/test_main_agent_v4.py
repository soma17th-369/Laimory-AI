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
    # 하루의 event: E1 집(06:00~08:30), E2 이동, E3 회사(점심 사진 P1), E4 회사 안의 회의 일정 C1.
    return json.dumps(
        {
            "conversations": [],
            "events": [
                {
                    "id": "E1", "eventType": "STAY", "title": "마포구 도화동에서 시간을 보냄",
                    "description": "{place}에서 시간을 보냈다.", "confidence": 0.7, "inferenceLevel": "DIRECT",
                    "reason": "집 체류 하나", "placeCandidates": ["한빛아파트"],
                },
                {
                    "id": "E2", "eventType": "MOVEMENT", "title": "출근", "description": "",
                    "confidence": 0.9, "inferenceLevel": "DIRECT", "reason": "이동 구간",
                },
                {
                    "id": "E3", "eventType": "WORK", "title": "순두부찌개와 만두를 먹음",
                    "description": "{place}에서 일하고 순두부찌개와 만두를 먹었다.", "confidence": 0.8,
                    "inferenceLevel": "EVIDENCE_BASED", "reason": "회사 체류와 점심 사진",
                    "placeCandidates": ["OO타워", "OO타워 주차장", "OO빌딩"],
                },
                {
                    "id": "E4", "eventType": "MEETING", "title": "주간 회의를 함",
                    "description": "{place}에서 주간 회의를 했다.", "confidence": 0.8,
                    "inferenceLevel": "DIRECT", "reason": "회의 일정",
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


def test_v4_pipeline_writes_text_on_the_events_the_code_made():
    request = _day()

    draft, timeline_llm = _run(request, _timeline_response())

    prompt = timeline_llm.calls[0].prompt
    assert "[event]" in prompt and '"id": "E3"' in prompt
    assert request.stays[0].raw_id not in prompt, "Timeline 입력에 rawId 를 싣지 않는다."

    by_type = {event.event_type: event for event in draft.events}
    assert set(by_type) == {EventType.STAY, EventType.MOVEMENT, EventType.WORK, EventType.MEETING}
    movement = by_type[EventType.MOVEMENT]
    assert movement.description == "", "코드는 이동 문장을 쓰지 않는다. Repair 가 쓴다(사용자 결정 2026-10-31)."
    assert movement.place is None

    work = by_type[EventType.WORK]
    assert work.description == "{place}에서 일하고 순두부찌개와 만두를 먹었다."
    assert work.place_candidates == ["OO타워", "OO빌딩", "OO식당"], (
        "Timeline 이 고른 순서 뒤에 나머지 이름이 붙고, 주차장은 후보가 되지 않는다."
    )
    photo_raw_id = request.photos[0].raw_id
    office_raw_id = request.stays[1].raw_id
    assert {office_raw_id, photo_raw_id} <= {ref.raw_id for ref in work.source_refs}, "점심 사진은 회사 방문의 것이다."
    assert (work.start_time.hour, work.start_time.minute, work.end_time.hour) == (9, 10, 18)
    assert work.base_event_ids == ["E3"] and work.merge_reason == "회사 체류와 점심 사진"

    meeting = by_type[EventType.MEETING]
    meeting_raw_id = request.calendars[0].raw_id
    assert meeting_raw_id in {ref.raw_id for ref in meeting.source_refs}
    assert photo_raw_id not in {ref.raw_id for ref in meeting.source_refs}, "시간만 겹친 일정은 사진을 갖지 않는다."
    assert (meeting.start_time.hour, meeting.end_time.hour) == (10, 11)


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
