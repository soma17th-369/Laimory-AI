"""Photo describe → infer agentic workflow 검증.

- description 이 null 인 사진은 describe 단계에서 배치로 채운다.
- 이미 description 이 있는 사진은 describe LLM 호출을 하지 않는다.
- 채워진 description 이 infer 단계 프롬프트에 반영된다.
"""

import json

from app.agents.events.photo.agent import PhotoEventAgent
from app.agents.events.photo.describer import LLMPhotoDescriber
from app.schemas import PhotoItem
from tests.fixtures.fake_llm import FakeLLM, candidate, result_json
from tests.fixtures.requests import fixture_raw_id, make_request

PHOTO_1 = fixture_raw_id("photo-1")
PHOTO_2 = fixture_raw_id("photo-2")

_DESCRIBE_RESPONSE = json.dumps(
    {"descriptions": [{"rawId": PHOTO_1, "description": "저녁 무렵 야외에서 남긴 사진으로 보인다."}]},
    ensure_ascii=False,
)
_INFER_RESPONSE = result_json(
    candidates=[candidate("PHOTO_MOMENT", [("PHOTO", PHOTO_1)])]
)


def test_describer_fills_only_missing_descriptions():
    photos = [
        PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", dateTaken=None),
        PhotoItem(rawId=PHOTO_2, takenAt="2026-06-20T19:00:00", description="이미 있는 설명"),
    ]
    llm = FakeLLM([_DESCRIBE_RESPONSE])

    out = LLMPhotoDescriber(llm).describe(photos)

    assert out == {PHOTO_1: "저녁 무렵 야외에서 남긴 사진으로 보인다."}
    assert len(llm.calls) == 1  # 대상은 사진 1장뿐이라도 배치 1회 호출


def test_describer_no_call_when_all_present():
    photos = [PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", description="있음")]
    llm = FakeLLM([_DESCRIBE_RESPONSE])

    assert LLMPhotoDescriber(llm).describe(photos) == {}
    assert llm.calls == []  # 채울 사진이 없으면 LLM 을 호출하지 않는다.


def test_agent_describes_then_infers_with_filled_description():
    request = make_request(
        photos=[PhotoItem(rawId=PHOTO_1, takenAt="2026-06-20T18:00:00", dateTaken=None)]
    )
    llm = FakeLLM([_DESCRIBE_RESPONSE, _INFER_RESPONSE])

    result = PhotoEventAgent(llm=llm).generate(request)

    # 호출 순서: describe → infer
    assert len(llm.calls) == 2
    # 생성된 description 이 infer 프롬프트(2번째 호출)에 반영된다.
    assert "저녁 무렵 야외에서 남긴 사진" in llm.calls[1].prompt
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
