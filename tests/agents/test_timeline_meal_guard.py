"""Timeline Agent 가 지나치게 긴 식사 event 를 실제로 잘라 내는지 검증.

LLM 이 3시간 카페 체류 + 음식 사진 한 장을 통째로 `MEAL` 로 묶어 돌려줘도, draft
repair 단계에서 식사 event 는 1시간 미만으로 줄어들어야 한다.
"""

import json
from datetime import timedelta

from app.schemas import AgentEventResult
from tests.fixtures.fake_llm import candidate
from tests.fixtures.pipeline import run_timeline_pipeline
from tests.fixtures.requests import make_request, photo_item, stay_item

STAY_START = "2026-06-20T12:00:00"
STAY_END = "2026-06-20T15:00:00"
PHOTO_TAKEN = "2026-06-20T13:20:00"


def _request():
    return make_request(
        stays=[stay_item(1, raw_id="stay-1", start=STAY_START, end=STAY_END)],
        photos=[photo_item(2, taken=PHOTO_TAKEN, raw_id="photo-1")],
    )


def _agent_result() -> AgentEventResult:
    return AgentEventResult.model_validate(
        {
            "candidates": [
                candidate(
                    "MEAL",
                    [("STAY", "stay-1"), ("PHOTO", "photo-1")],
                    start=f"{STAY_START}+09:00",
                    end=f"{STAY_END}+09:00",
                )
            ],
            "fragments": [],
        }
    )


def _response(event_type: str) -> str:
    """LLM 이 3시간 체류 전체를 하나의 event 로 묶어 돌려준 draft."""

    return json.dumps(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                {
                    "eventType": event_type,
                    "title": "카페에서 점심 식사",
                    "description": "카페에 앉아 점심을 먹었다.",
                    "startTime": f"{STAY_START}+09:00",
                    "endTime": f"{STAY_END}+09:00",
                    "confidence": 0.85,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [
                        {"sourceType": "STAY", "rawId": "stay-1"},
                        {"sourceType": "PHOTO", "rawId": "photo-1"},
                    ],
                    "uncertainty": [],
                }
            ],
            "questions": [],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def test_three_hour_stay_with_food_photo_is_not_a_three_hour_meal():
    draft = run_timeline_pipeline(_request(), _agent_result(), _response("MEAL"))

    meal = draft.events[0]
    duration = meal.end_time - meal.start_time

    # "3시간 동안 점심 식사" 는 나오지 않는다.
    assert duration < timedelta(hours=1)
    # 식사 시각은 음식 사진 촬영 시각에 붙는다.
    assert meal.start_time.isoformat() == "2026-06-20T13:20:00+09:00"
    assert any("식사 event 시간을" in warning.message for warning in draft.warnings)
    assert meal.uncertainty


def test_the_same_three_hour_stay_as_rest_keeps_its_full_span():
    draft = run_timeline_pipeline(_request(), _agent_result(), _response("REST"))

    event = draft.events[0]

    # 식사가 아닌 체류는 3시간 그대로 둔다. 가드는 MEAL 에만 적용된다.
    assert event.end_time - event.start_time == timedelta(hours=3)
    assert draft.warnings == []
