import json

from app.agents.events.photo.agent import PhotoEventAgent, _photo_items_to_text
from app.schemas import PhotoItem
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import make_request


def test_source_ref_accepts_legacy_source_id_and_serializes_raw_id() -> None:
    from app.schemas import SourceRef

    ref = SourceRef.model_validate({"sourceType": "PHOTO", "sourceId": "photo-123"})

    assert ref.source_id == "photo-123"
    assert ref.model_dump(by_alias=True, mode="json") == {
        "sourceType": "PHOTO",
        "rawId": "photo-123",
        "reason": None,
    }


def test_photo_payload_treats_taken_at_as_shooting_time() -> None:
    photo = PhotoItem(
        id=1,
        takenAt="2026-06-20T12:00:00",
        dateTaken=None,
        filename="shot.jpg",
        description="음식이 놓인 식탁 사진이다.",
    )

    payload = json.loads(_photo_items_to_text([photo]))

    assert payload[0]["dateTaken"] is None
    assert "isDownloaded" not in payload[0]
    assert "timelineTimeUsable" not in payload[0]
    assert payload[0]["recommendedUse"] == "MERGE_WITH_STAY_OR_CALENDAR"
    assert "actual shooting time" in payload[0]["timePolicy"]
    assert payload[0]["photoMeaning"]["description"] == "음식이 놓인 식탁 사진이다."
    assert payload[0]["photoMeaning"]["useForActivityInference"] is True


def test_photo_description_semantics_are_accepted_in_candidate_contract() -> None:
    response = json.dumps(
        {
            "candidates": [
                {
                    "eventType": "PHOTO_MOMENT",
                    "timeRange": {
                        "startTime": "2026-06-20T12:00:00+09:00",
                        "endTime": "2026-06-20T12:00:00+09:00",
                    },
                    "title": "점심 식사 사진",
                    "description": "식탁 사진으로 보아 점심을 먹은 흔적이 있다.",
                    "evidenceSummary": "사진 description에 음식이 놓인 식탁이 보인다.",
                    "semanticTags": ["식사", "음식"],
                    "sourceRefs": [
                        {
                            "sourceType": "PHOTO",
                            "rawId": "1",
                            "reason": "같은 시간대에 해당 장소에서 촬영된 사진이며 음식 사진 설명이 포함됨",
                        }
                    ],
                    "confidence": 0.4,
                    "inferenceLevel": "UNCERTAIN",
                    "uncertainty": ["사진만으로 식사 시간을 확정하기는 어려움"],
                }
            ],
            "fragments": [],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM([response])
    request = make_request(
        photos=[
            PhotoItem(
                id=1,
                takenAt="2026-06-20T12:00:00",
                dateTaken=None,
                description="음식이 놓인 식탁 사진이다.",
            )
        ]
    )

    result = PhotoEventAgent(llm=llm).generate(request)

    assert result.candidates[0].evidence_summary == "사진 description에 음식이 놓인 식탁이 보인다."
    assert result.candidates[0].semantic_tags == ["식사", "음식"]
    assert (
        result.candidates[0].source_refs[0].reason
        == "같은 시간대에 해당 장소에서 촬영된 사진이며 음식 사진 설명이 포함됨"
    )
    dumped_ref = result.candidates[0].source_refs[0].model_dump(by_alias=True)
    assert dumped_ref["rawId"] == "1"
    assert "sourceId" not in dumped_ref
    assert '"timelineTimeUsable"' not in llm.calls[0].prompt
    assert '"isDownloaded"' not in llm.calls[0].prompt
    assert "MERGE_WITH_STAY_OR_CALENDAR" in llm.calls[0].prompt
