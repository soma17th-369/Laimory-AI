"""Photo describe → infer agentic workflow 검증.

- description 이 null 인 사진은 describe 단계에서 채운다.
- 이미지를 구하지 못하면 **코드가** 메타데이터로 채운다(#56 §12). LLM 을 부르지 않는다.
- 채워진 description 이 infer 단계 프롬프트에 반영된다.
"""

from app.agents.events.photo.agent import PhotoEventAgent
from app.agents.events.photo.describer import MetadataPhotoDescriber
from app.schemas import PhotoItem
from tests.fixtures.fake_llm import FakeLLM, candidate, result_json
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

PHOTO_1 = fixture_raw_id("photo-1")
PHOTO_2 = fixture_raw_id("photo-2")

_INFER_RESPONSE = result_json(
    candidates=[candidate("PHOTO_MOMENT", [("PHOTO", PHOTO_1)])]
)


def test_metadata_describer_fills_only_missing_descriptions():
    photos = [
        PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", dateTaken=None),
        PhotoItem(rawId=PHOTO_2, takenAt="2026-06-20T19:00:00", description="이미 있는 설명"),
    ]

    out = MetadataPhotoDescriber().describe(photos)

    assert set(out) == {PHOTO_1}
    assert "18:00" in out[PHOTO_1]
    # 이미지를 못 봤다는 사실을 숨기지 않는다. 이후 추론이 이 한계를 알아야 한다.
    assert "알 수 없다" in out[PHOTO_1]


def test_metadata_describer_uses_stay_place_at_shooting_time():
    """§12 "기존 장소 정보" — 촬영 시각을 덮는 체류가 있으면 그 장소를 적는다."""

    photos = [PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T12:30:00", dateTaken=None)]
    stays = [
        stay_item(
            "stay-1",
            start="2026-06-20T12:00:00",
            end="2026-06-20T13:00:00",
            place="두꺼비 감자탕 지산점",
        )
    ]

    out = MetadataPhotoDescriber(stays).describe(photos)

    assert "두꺼비 감자탕 지산점" in out[PHOTO_1]


def test_metadata_describer_does_not_invent_place_without_stay():
    photos = [PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T12:30:00", dateTaken=None)]

    out = MetadataPhotoDescriber().describe(photos)

    assert "에서 촬영된 것으로 보인다" not in out[PHOTO_1]


def test_metadata_describer_returns_nothing_when_all_present():
    photos = [PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", description="있음")]

    assert MetadataPhotoDescriber().describe(photos) == {}


def test_agent_describes_with_code_then_infers():
    """코드 describer 를 쓰면 describe 단계에서 LLM 호출이 없다. infer 만 남는다."""

    request = make_request(
        photos=[PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", dateTaken=None)]
    )
    llm = FakeLLM([_INFER_RESPONSE])

    result = PhotoEventAgent(llm=llm, describer=MetadataPhotoDescriber()).generate(
        request
    )

    assert len(llm.calls) == 1
    assert "18:00" in llm.calls[0].prompt
    assert result.candidates and result.candidates[0].source_refs[0].raw_id == PHOTO_1


def test_agent_skips_describe_when_description_present():
    request = make_request(
        photos=[PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", description="식탁 사진")]
    )
    llm = FakeLLM([_INFER_RESPONSE])

    result = PhotoEventAgent(llm=llm).generate(request)

    assert len(llm.calls) == 1  # describe 호출 없음 → infer 만
    assert "식탁 사진" in llm.calls[0].prompt
    assert result.candidates
