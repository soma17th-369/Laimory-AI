import json

from app.schemas import AgentEventResult
from tests.fixtures.fake_llm import candidate
from tests.fixtures.pipeline import run_timeline_pipeline
from tests.fixtures.requests import make_request, stay_item


def test_timeline_agent_drops_outside_events_and_clamps_partial_events() -> None:
    request = make_request(
        window={
            "start": "2026-06-20T10:00:00",
            "end": "2026-06-20T12:00:00",
        },
        # draft 가 참조하는 rawId 를 실제로 담아, window 검증만 분리해 확인한다.
        stays=[stay_item(1, raw_id="stay-1"), stay_item(2, raw_id="stay-2")],
    )
    agent_result = AgentEventResult.model_validate(
        {
            "candidates": [
                candidate(
                    "MOVEMENT",
                    [("STAY", "stay-1")],
                    start="2026-06-20T10:10:00+09:00",
                    end="2026-06-20T10:20:00+09:00",
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
                    "eventType": "MOVEMENT",
                    "title": "window 경계에 걸친 이동",
                    "description": "",
                    "startTime": "2026-06-20T09:30:00+09:00",
                    "endTime": "2026-06-20T10:30:00+09:00",
                    "confidence": 0.7,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [{"sourceType": "STAY", "sourceId": "stay-1"}],
                    "uncertainty": [],
                },
                {
                    "eventType": "MOVEMENT",
                    "title": "window 밖 이동",
                    "description": "",
                    "startTime": "2026-06-20T13:00:00+09:00",
                    "endTime": "2026-06-20T14:00:00+09:00",
                    "confidence": 0.7,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [{"sourceType": "STAY", "sourceId": "stay-2"}],
                    "uncertainty": [],
                },
            ],
            "questions": [],
            "warnings": [],
        },
        ensure_ascii=False,
    )

    draft = run_timeline_pipeline(request, agent_result, response)

    assert len(draft.events) == 1
    assert draft.events[0].client_event_id == "event-001"
    assert draft.events[0].start_time.isoformat() == "2026-06-20T10:00:00+09:00"
    assert draft.events[0].end_time.isoformat() == "2026-06-20T10:30:00+09:00"
    assert any("window" in warning.message for warning in draft.warnings)


