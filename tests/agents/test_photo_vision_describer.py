"""Vision 기반 사진 description 생성 + 실제 이미지 파일 로딩 검증.

- `LocalFilePhotoImageSource` 가 실제 이미지 파일(bytes)을 읽는다.
- 실제 저장 파일명은 `photoFile`(→`clientPhotoUri`)과 일치하므로, 소스는
  `clientPhotoUri` → `filename` 순으로 파일을 찾는다.
- `VisionPhotoDescriber` 가 이미지를 vision 호출에 실어 보내고, 이미지를 못 구한
  사진은 메타데이터 fallback 으로 채운다.
"""

import json
from pathlib import Path

from app.agents.events.photo import image_source as image_source_module
from app.agents.events.photo.agent import PhotoEventAgent
from app.agents.events.photo.describer import LLMPhotoDescriber, VisionPhotoDescriber
from app.agents.events.photo.image_source import (
    LocalFilePhotoImageSource,
    NullPhotoImageSource,
    S3PhotoImageSource,
    default_photo_image_source,
)
from app.core.llm import ImageInput
from app.schemas import PhotoItem
from tests.fixtures.fake_llm import FakeLLM, candidate, result_json
from tests.fixtures.requests import make_request

IMAGES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "images"
# data/input 에 실제 촬영 이미지가 들어와 있어, 실제 바이너리 로딩을 이 파일로 검증한다.
DATA_INPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "input"
REAL_PHOTO_FILE = "000_20260708_172720.jpg"  # = 입력 JSON 의 photoFile(=clientPhotoUri)
_JPEG_SIGNATURE = b"\xff\xd8\xff"

_VISION_RESPONSE = json.dumps(
    {"descriptions": [{"id": 1, "description": "빨간 배경의 단색 이미지."}]},
    ensure_ascii=False,
)
_REAL_VISION_RESPONSE = json.dumps(
    {"descriptions": [{"id": 1, "description": "실제 촬영 사진에서 읽은 설명."}]},
    ensure_ascii=False,
)
_META_RESPONSE = json.dumps(
    {"descriptions": [{"id": 2, "description": "메타데이터로 추정한 사진."}]},
    ensure_ascii=False,
)


def test_local_file_image_source_reads_real_file():
    source = LocalFilePhotoImageSource(IMAGES_DIR)
    photo = PhotoItem(id=1, takenAt="2026-06-20T18:00:00", filename="sample.png")

    image = source.load(photo)

    assert isinstance(image, ImageInput)
    assert image.mime_type == "image/png"
    assert image.data[:8] == b"\x89PNG\r\n\x1a\n"  # 실제 PNG 시그니처


def test_local_file_image_source_missing_returns_none():
    source = LocalFilePhotoImageSource(IMAGES_DIR)
    photo = PhotoItem(id=9, takenAt="2026-06-20T18:00:00", filename="does-not-exist.png")

    assert source.load(photo) is None


def test_local_file_image_source_reads_real_input_by_client_photo_uri():
    # 실제 저장 파일명은 photoFile(→clientPhotoUri)과 일치한다. filename 은 접두어가
    # 없어 디스크에 없으므로, clientPhotoUri 로 실제 파일을 찾아야 한다.
    source = LocalFilePhotoImageSource(DATA_INPUT_DIR)
    photo = PhotoItem(
        id=1,
        takenAt="2026-07-08T17:27:20.309",
        clientPhotoUri=REAL_PHOTO_FILE,
        filename="20260708_172720.jpg",  # 접두어 없어 디스크에 없음 → clientPhotoUri 우선
    )

    image = source.load(photo)

    assert isinstance(image, ImageInput)
    assert image.mime_type == "image/jpeg"
    assert image.data[:3] == _JPEG_SIGNATURE  # 실제 JPEG 시그니처


def test_vision_describer_sends_real_image_and_parses():
    source = LocalFilePhotoImageSource(IMAGES_DIR)
    llm = FakeLLM([_VISION_RESPONSE])
    photo = PhotoItem(id=1, takenAt="2026-06-20T18:00:00", filename="sample.png")

    out = VisionPhotoDescriber(image_source=source, llm=llm).describe([photo])

    assert out == {1: "빨간 배경의 단색 이미지."}
    # 실제 이미지 bytes 가 vision 호출에 실렸다.
    assert len(llm.calls) == 1
    assert llm.calls[0].images and isinstance(llm.calls[0].images[0], ImageInput)
    assert llm.calls[0].images[0].data[:8] == b"\x89PNG\r\n\x1a\n"


def test_vision_describer_sends_real_input_image():
    # data/input 의 실제 JPEG 를 clientPhotoUri 로 로딩해 vision 호출에 실어 보낸다.
    source = LocalFilePhotoImageSource(DATA_INPUT_DIR)
    llm = FakeLLM([_REAL_VISION_RESPONSE])
    photo = PhotoItem(
        id=1,
        takenAt="2026-07-08T17:27:20.309",
        clientPhotoUri=REAL_PHOTO_FILE,
        filename="20260708_172720.jpg",
    )

    out = VisionPhotoDescriber(image_source=source, llm=llm).describe([photo])

    assert out == {1: "실제 촬영 사진에서 읽은 설명."}
    assert len(llm.calls) == 1
    sent = llm.calls[0].images
    assert sent and isinstance(sent[0], ImageInput)
    assert sent[0].mime_type == "image/jpeg"
    assert sent[0].data[:3] == _JPEG_SIGNATURE  # 실제 JPEG bytes 가 실렸다.


def test_vision_describer_falls_back_to_metadata_without_image():
    # 이미지 소스가 항상 None → 메타데이터 fallback 으로 채운다.
    llm = FakeLLM([_META_RESPONSE])
    photo = PhotoItem(id=2, takenAt="2026-06-20T18:00:00", filename="nope.png")

    out = VisionPhotoDescriber(
        image_source=NullPhotoImageSource(),
        llm=llm,
        fallback=LLMPhotoDescriber(llm),
    ).describe([photo])

    assert out == {2: "메타데이터로 추정한 사진."}
    # vision 호출이 아니라 텍스트(complete) 호출로 처리됐다.
    assert len(llm.calls) == 1
    assert llm.calls[0].images is None


def test_default_photo_image_source_uses_config_dir(monkeypatch):
    # settings.photo_image_dir 이 지정되면 로컬 실제 파일을 읽는 소스가 된다.
    monkeypatch.setattr(
        image_source_module.settings, "photo_image_dir", str(DATA_INPUT_DIR)
    )

    source = default_photo_image_source()

    assert isinstance(source, LocalFilePhotoImageSource)
    image = source.load(
        PhotoItem(id=1, takenAt="2026-07-08T17:27:20.309", clientPhotoUri=REAL_PHOTO_FILE)
    )
    assert image is not None and image.data[:3] == _JPEG_SIGNATURE


def test_default_photo_image_source_is_null_without_config(monkeypatch):
    # 설정이 비어 있으면(기본) 이미지 없음 → 메타데이터 fallback 으로 동작한다.
    monkeypatch.setattr(image_source_module.settings, "photo_image_dir", None)

    assert isinstance(default_photo_image_source(), NullPhotoImageSource)


def test_photo_agent_default_describer_loads_real_image_via_config(monkeypatch):
    # 프로덕션 기본 경로: PhotoEventAgent → VisionPhotoDescriber(설정 이미지 소스).
    # photo_image_dir 이 data/input 이면 실제 JPEG 를 vision 호출로 보고 describe 한다.
    monkeypatch.setattr(
        image_source_module.settings, "photo_image_dir", str(DATA_INPUT_DIR)
    )
    infer_response = result_json(
        candidates=[candidate("PHOTO_MOMENT", [("PHOTO", "1")])]
    )
    llm = FakeLLM([_REAL_VISION_RESPONSE, infer_response])
    request = make_request(
        photos=[
            PhotoItem(
                id=1,
                takenAt="2026-07-08T17:27:20.309",
                clientPhotoUri=REAL_PHOTO_FILE,
                filename="20260708_172720.jpg",
            )
        ]
    )

    result = PhotoEventAgent(llm=llm).generate(request)

    # describe(vision, 실제 이미지) → infer 순서로 2회 호출
    assert len(llm.calls) == 2
    assert llm.calls[0].images and llm.calls[0].images[0].data[:3] == _JPEG_SIGNATURE
    assert "실제 촬영 사진에서 읽은 설명" in llm.calls[1].prompt
    assert result.candidates


def test_s3_image_source_is_skeleton_not_connected():
    photo = PhotoItem(id=1, takenAt="2026-06-20T18:00:00", filename="sample.png")
    try:
        S3PhotoImageSource(bucket="b").load(photo)
        raised = False
    except NotImplementedError:
        raised = True
    assert raised
