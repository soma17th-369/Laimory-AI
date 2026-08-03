"""Vision 기반 사진 description 생성 (이슈 #52).

- `VisionPhotoDescriber` 가 이미지 소스에서 받은 bytes 를 vision 호출에 싣는다.
- 이미지를 못 구한 사진은 메타데이터 fallback 으로 채워, 일부만 성공해도 설명이 비지 않는다.
- `PhotoEventAgent` 기본 경로가 `photoUrl` 다운로드 소스로 조립된다.
- `photoUrl` 값은 LLM 프롬프트 어디에도 실리지 않는다.
"""

import json

import httpx
import pytest

from app.agents.events.photo import agent as agent_module
from app.agents.events.photo import image_source as image_source_module
from app.agents.events.photo.agent import PhotoEventAgent
from app.agents.events.photo.describer import (
    MetadataPhotoDescriber,
    VisionPhotoDescriber,
)
from app.agents.events.photo.image_source import (
    NullPhotoImageSource,
    PhotoImageSource,
    PhotoUrlImageSource,
)
from app.core.llm import ImageInput
from app.schemas import PhotoItem
from tests.fixtures.fake_llm import FakeLLM, candidate, result_json
from tests.fixtures.requests import fixture_raw_id, make_request

PHOTO_1 = fixture_raw_id("photo-1")
PHOTO_2 = fixture_raw_id("photo-2")
JPEG = b"\xff\xd8\xff" + b"0" * 64
PHOTO_URL = "https://images.example.com/a.jpg?X-Amz-Signature=deadbeefcafe"

_VISION_RESPONSE = json.dumps(
    {"descriptions": [{"rawId": PHOTO_1, "description": "카페 테이블 위의 커피잔."}]},
    ensure_ascii=False,
)
_META_RESPONSE = json.dumps(
    {"descriptions": [{"rawId": PHOTO_2, "description": "메타데이터로 추정한 사진."}]},
    ensure_ascii=False,
)


class StubImageSource(PhotoImageSource):
    """rawId → 이미지 매핑을 그대로 돌려주는 테스트용 소스."""

    def __init__(self, images: dict[str, ImageInput]) -> None:
        self._images = images

    def load(self, photo: PhotoItem) -> ImageInput | None:
        return self._images.get(photo.raw_id)


def photo(raw_id: str, *, url: str | None = PHOTO_URL) -> PhotoItem:
    return PhotoItem(
        rawId=raw_id, takenAt="2026-07-31T17:47:00+09:00", photoUrl=url
    )


def test_vision_describer_sends_image_bytes():
    source = StubImageSource({PHOTO_1: ImageInput(data=JPEG, mime_type="image/jpeg")})
    llm = FakeLLM([_VISION_RESPONSE])

    out = VisionPhotoDescriber(image_source=source, llm=llm).describe([photo(PHOTO_1)])

    assert out == {PHOTO_1: "카페 테이블 위의 커피잔."}
    assert len(llm.calls) == 1
    sent = llm.calls[0].images
    assert sent and isinstance(sent[0], ImageInput)
    assert sent[0].data == JPEG
    assert sent[0].mime_type == "image/jpeg"


def test_metadata_fallback_does_not_call_llm():
    """이미지를 못 구한 사진은 코드가 채운다(#56 §12). vision 호출만 남는다.

    예전에는 이 자리에서도 LLM 을 불러 "메타데이터로 설명을 추정" 하게 했다. 이미지를
    보지 못한 채 장면을 지어내는 구조였고, 그 문장이 event 추론의 근거가 됐다.
    """

    source = StubImageSource({PHOTO_1: ImageInput(data=JPEG, mime_type="image/jpeg")})
    llm = FakeLLM([_VISION_RESPONSE])

    out = VisionPhotoDescriber(
        image_source=source, llm=llm, fallback=MetadataPhotoDescriber()
    ).describe([photo(PHOTO_1), photo(PHOTO_2)])

    assert out[PHOTO_1] == "카페 테이블 위의 커피잔."
    assert "알 수 없다" in out[PHOTO_2]
    # 호출은 vision 한 번뿐이다. 메타데이터 경로는 LLM 을 쓰지 않는다.
    assert len(llm.calls) == 1
    assert llm.calls[0].images is not None
    assert "실제 이미지가 첨부됩니다" in llm.calls[0].system


def test_vision_describer_falls_back_to_metadata_without_image(caplog):
    # 이미지 소스가 항상 None → 코드 기반 메타데이터 fallback 으로 채운다.
    llm = FakeLLM([_VISION_RESPONSE])

    with caplog.at_level("DEBUG"):
        out = VisionPhotoDescriber(
            image_source=NullPhotoImageSource(),
            llm=llm,
            fallback=MetadataPhotoDescriber(),
        ).describe([photo(PHOTO_2)])

    assert "알 수 없다" in out[PHOTO_2]
    assert llm.calls == []  # LLM 을 전혀 부르지 않는다.
    assert not [
        record
        for record in caplog.records
        if record.name == "app.agents.events.photo.image_source"
        and record.levelno >= 20
    ]


def test_vision_describer_mixes_vision_and_fallback():
    # 한 장만 이미지를 구한 경우: vision 1회 + fallback 1회로 둘 다 채운다.
    source = StubImageSource({PHOTO_1: ImageInput(data=JPEG, mime_type="image/jpeg")})
    llm = FakeLLM([_VISION_RESPONSE, _META_RESPONSE])

    out = VisionPhotoDescriber(image_source=source, llm=llm).describe(
        [photo(PHOTO_1), photo(PHOTO_2)]
    )

    assert out == {
        PHOTO_1: "카페 테이블 위의 커피잔.",
        PHOTO_2: "메타데이터로 추정한 사진.",
    }
    assert len(llm.calls) == 2
    assert llm.calls[0].images is not None  # vision
    assert llm.calls[1].images is None  # fallback


def test_vision_call_failure_falls_back_to_metadata():
    source = StubImageSource({PHOTO_1: ImageInput(data=JPEG, mime_type="image/jpeg")})
    metadata_response = json.dumps(
        {
            "descriptions": [
                {"rawId": PHOTO_1, "description": "메타데이터로 추정한 사진."}
            ]
        },
        ensure_ascii=False,
    )
    llm = FakeLLM([RuntimeError("vision failed"), metadata_response])

    out = VisionPhotoDescriber(image_source=source, llm=llm).describe(
        [photo(PHOTO_1)]
    )

    assert out == {PHOTO_1: "메타데이터로 추정한 사진."}
    assert len(llm.calls) == 2
    assert llm.calls[0].images is not None
    assert llm.calls[1].images is None


def test_incomplete_vision_response_falls_back_for_missing_photo():
    source = StubImageSource(
        {
            PHOTO_1: ImageInput(data=JPEG, mime_type="image/jpeg"),
            PHOTO_2: ImageInput(data=JPEG, mime_type="image/jpeg"),
        }
    )
    llm = FakeLLM([_VISION_RESPONSE, _META_RESPONSE])

    out = VisionPhotoDescriber(image_source=source, llm=llm).describe(
        [photo(PHOTO_1), photo(PHOTO_2)]
    )

    assert out == {
        PHOTO_1: "카페 테이블 위의 커피잔.",
        PHOTO_2: "메타데이터로 추정한 사진.",
    }
    assert len(llm.calls) == 2


def test_describer_skips_photos_that_already_have_description():
    llm = FakeLLM(["{}"])
    described = PhotoItem(
        rawId=PHOTO_1, takenAt="2026-07-31T17:47:00+09:00", description="이미 있음"
    )

    out = VisionPhotoDescriber(image_source=NullPhotoImageSource(), llm=llm).describe(
        [described]
    )

    assert out == {}
    assert llm.calls == []  # 채울 게 없으면 LLM 을 부르지 않는다.


def test_photo_agent_default_path_downloads_and_describes(monkeypatch):
    """운영 기본 경로: PhotoEventAgent → VisionPhotoDescriber → photoUrl 다운로드."""

    monkeypatch.setattr(
        image_source_module.settings,
        "photo_url_allowed_hosts",
        "images.example.com",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    # 기본 조립이 URL 소스인지 확인한 뒤, 전송만 가짜로 바꿔 끼운다.
    # agent 가 `from ... import default_photo_image_source` 로 이름을 가져가므로
    # 정의 모듈이 아니라 agent 모듈의 이름을 바꿔야 한다.
    assert isinstance(image_source_module.default_photo_image_source(), PhotoUrlImageSource)
    monkeypatch.setattr(
        agent_module,
        "default_photo_image_source",
        lambda: PhotoUrlImageSource(transport=httpx.MockTransport(handler)),
    )

    infer_response = result_json(
        candidates=[candidate("PHOTO_MOMENT", [("PHOTO", PHOTO_1)])]
    )
    llm = FakeLLM([_VISION_RESPONSE, infer_response])
    request = make_request(photos=[photo(PHOTO_1)])

    result = PhotoEventAgent(llm=llm).generate(request)

    # describe(vision, 내려받은 bytes) → infer 순서로 2회 호출
    assert len(llm.calls) == 2
    assert llm.calls[0].images and llm.calls[0].images[0].data == JPEG
    assert "카페 테이블 위의 커피잔" in llm.calls[1].prompt
    assert result.candidates


@pytest.mark.parametrize("has_image", [True, False])
def test_photo_url_never_reaches_the_prompt(has_image):
    """presigned URL 은 어떤 프롬프트에도 실리지 않는다.

    `PhotoItem.photo_url` 이 `exclude=True` 라 `model_dump()` 경로에서 빠지고,
    describe 프롬프트도 URL 을 넣지 않는다.
    """

    images = {PHOTO_1: ImageInput(data=JPEG, mime_type="image/jpeg")} if has_image else {}
    infer_response = result_json(
        candidates=[candidate("PHOTO_MOMENT", [("PHOTO", PHOTO_1)])]
    )
    llm = FakeLLM([_VISION_RESPONSE, infer_response])
    request = make_request(photos=[photo(PHOTO_1)])

    PhotoEventAgent(
        llm=llm,
        describer=VisionPhotoDescriber(image_source=StubImageSource(images), llm=llm),
    ).generate(request)

    for call in llm.calls:
        assert PHOTO_URL not in call.prompt
        assert "X-Amz-Signature" not in call.prompt
        assert "photoUrl" not in call.prompt
