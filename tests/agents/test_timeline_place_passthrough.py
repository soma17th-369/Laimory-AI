"""Timeline Agent 가 쓴 장소·주소·태그가 확정 pass 를 지나 그대로 남는지 검증.

draft 의 `place`/`address` 는 근거에 실재해야 한다. 근거로 뒷받침되는 값은 확정 pass 가
건드리지 않고, LLM 이 쓴 tags 도 그대로 이어진다.
"""

import json

from app.schemas import AgentEventResult
from tests.fixtures.fake_llm import candidate
from tests.fixtures.pipeline import run_timeline_pipeline
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


def test_supported_place_address_and_tags_survive_the_pipeline() -> None:
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
            "events": [
                {
                    "eventType": "CALENDAR_EVENT",
                    "title": "강남역 일정",
                    "description": "강남역에서 일정을 보냈어요.",
                    "address": "서울 강남구 강남대로 지하 396",
                    "place": "강남역",
                    "tags": ["#이동", "#일정"],
                    "startTime": "2026-06-20T15:00:00+09:00",
                    "endTime": "2026-06-20T16:00:00+09:00",
                    "confidence": 0.6,
                    "inferenceLevel": "UNCERTAIN",
                    "sourceRefs": [
                        {
                            "sourceType": "STAY",
                            "rawId": fixture_raw_id("stay-1"),
                            "reason": "같은 시간대에 강남역 위치 기록이 있음",
                        }
                    ],
                    "uncertainty": ["일정 제목이 없어 목적 확인 필요"],
                }
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )

    draft = run_timeline_pipeline(request, agent_result, response)

    assert draft.events[0].address == "서울 강남구 강남대로 지하 396"
    assert draft.events[0].place == "강남역"
    assert draft.events[0].tags == ["#이동", "#일정"]
