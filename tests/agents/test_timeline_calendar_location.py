"""Timeline Agent 가 캘린더+체류 병합 event 의 confidence 를 보강하는지 검증.

병합 판단은 프롬프트가 하고, 두 근거가 같은 장소를 가리키는지 확인해 confidence 를
올리는 것은 코드가 한다. 여기서는 LLM 이 이미 병합해 돌려준 draft 를 가정한다.
"""

import json

import pytest

from app.schemas import AgentEventResult
from tests.fixtures.fake_llm import candidate
from tests.fixtures.pipeline import run_timeline_pipeline
from tests.fixtures.requests import (
    calendar_item,
    fixture_raw_id,
    make_request,
    stay_item,
)

START = "2026-06-20T09:00:00+09:00"
END = "2026-06-20T10:00:00+09:00"

HOME_LOCATION_TEXT = "집(경기도 오산시 운암로 90)"
HOME_ADDRESS = "경기도 오산시 운암로 90"


def _request(location_text=HOME_LOCATION_TEXT):
    return make_request(
        calendars=[
            calendar_item(1, "ASM 개발", raw_id="cal-1", location_text=location_text)
        ],
        stays=[
            stay_item(
                2,
                raw_id="stay-1",
                place="오산운암3단지 주공아파트",
                address=HOME_ADDRESS,
                places=[],
            )
        ],
    )


def _agent_result() -> AgentEventResult:
    return AgentEventResult.model_validate(
        {
            "candidates": [
                candidate("CALENDAR_EVENT", [("CALENDAR", "cal-1")], start=START, end=END)
            ],
            "fragments": [],
        }
    )


def _merged_draft_response(refs, confidence=0.7) -> str:
    return json.dumps(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                {
                    "eventType": "CALENDAR_EVENT",
                    "title": "집에서 ASM 개발",
                    "description": "",
                    "startTime": START,
                    "endTime": END,
                    "confidence": confidence,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [
                        {"sourceType": st, "rawId": fixture_raw_id(raw_id)}
                        for st, raw_id in refs
                    ],
                    "uncertainty": [],
                }
            ],
            "questions": [],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def test_merged_calendar_and_stay_event_gets_a_confidence_boost():
    response = _merged_draft_response(
        [("CALENDAR", "cal-1"), ("STAY", "stay-1")], confidence=0.7
    )

    draft = run_timeline_pipeline(_request(), _agent_result(), response)

    # locationText `집(경기도 오산시 운암로 90)` ↔ stay.address 가 같은 장소를 가리킨다.
    assert draft.events[0].confidence == pytest.approx(0.8)
    assert len(draft.events[0].source_refs) == 2


def test_calendar_only_event_is_not_boosted():
    response = _merged_draft_response([("CALENDAR", "cal-1")], confidence=0.7)

    draft = run_timeline_pipeline(_request(), _agent_result(), response)

    # 체류 근거가 없으면 확증할 것이 없다.
    assert draft.events[0].confidence == pytest.approx(0.7)


def test_conflicting_places_are_not_boosted():
    response = _merged_draft_response(
        [("CALENDAR", "cal-1"), ("STAY", "stay-1")], confidence=0.7
    )
    request = _request(location_text="서울 강남구 테헤란로 152")

    draft = run_timeline_pipeline(request, _agent_result(), response)

    assert draft.events[0].confidence == pytest.approx(0.7)
