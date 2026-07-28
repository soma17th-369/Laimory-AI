import json

from app.schemas import AgentEventResult
from tests.fixtures.fake_llm import candidate
from tests.fixtures.pipeline import run_timeline_pipeline
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


def test_timeline_question_is_rewritten_to_event_specific_sentence() -> None:
    # draft 가 쓸 장소/주소는 근거에 실재해야 한다. 없으면 repair 가 지운다.
    request = make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-1",
                place="강남역",
                address="서울 강남구 강남대로 지하 396",
                places=[],
            )
        ]
    )
    agent_result = AgentEventResult.model_validate(
        {
            "candidates": [
                candidate(
                    "CALENDAR_EVENT",
                    [("STAY", "stay-1")],
                    start="2026-06-20T15:00:00+09:00",
                    end="2026-06-20T16:00:00+09:00",
                )
            ],
            "fragments": [],
        }
    )
    response = json.dumps(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                {
                    "eventType": "CALENDAR_EVENT",
                    "title": "강남역 일정",
                    "description": "강남역 근처에 머문 일정으로 보인다.",
                    "address": "서울 강남구 강남대로 지하 396",
                    "placeLabel": "강남역",
                    "tags": ["#이동", "#일정"],
                    "startTime": "2026-06-20T15:00:00+09:00",
                    "endTime": "2026-06-20T16:00:00+09:00",
                    "confidence": 0.6,
                    "inferenceLevel": "UNCERTAIN",
                    "sourceRefs": [
                        {
                            "sourceType": "STAY",
                                "rawId": fixture_raw_id("stay-1"),
                            "reason": "같은 시간대에 강남역 근처 위치 기록이 있음",
                        }
                    ],
                    "uncertainty": ["일정 제목이 없어 목적 확인 필요"],
                }
            ],
            "questions": [
                {
                    "timeRange": {
                        "startTime": "2026-06-20T15:00:00+09:00",
                        "endTime": "2026-06-20T16:00:00+09:00",
                    },
                    "question": "시간 확인 필요",
                    "reason": "위치 기록만으로 일정 목적을 확정하기 어려움",
                    "relatedEventIds": ["event-001"],
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )

    draft = run_timeline_pipeline(request, agent_result, response)

    assert draft.events[0].address == "서울 강남구 강남대로 지하 396"
    assert draft.events[0].place_label == "강남역"
    assert draft.events[0].tags == ["#이동", "#일정"]
    assert draft.questions[0].question == "오후 3시쯤 강남역 일정 활동이 맞나요?"

