"""사진 description 생성기 (PhotoEventAgent 의 describe 단계).

수집 입력의 사진 `description` 은 보통 `null` 로 온다. PhotoEventAgent 가 event
를 추론하기 전에, description 이 비어 있는 사진들의 설명을 여기서 채운다.

description 은 궁극적으로 **실제 사진**(예: S3 에 저장된 이미지)을 vision 모델로
보고 만들어야 정확하다. 아직 이미지 저장/조회(S3)가 정해지지 않아, 이 모듈은
`PhotoDescriber` 인터페이스만 고정하고 기본 구현으로 **메타데이터 기반 배치
설명 생성기**(`LLMPhotoDescriber`)를 둔다. S3 + vision 이 준비되면 같은
인터페이스를 구현하는 describer(이미지를 내려받아 vision 모델에 전달)로 교체하고,
PhotoEventAgent 생성 시 주입만 바꾸면 된다.

호출 단위는 **배치 1회**다. description 이 필요한 사진 전부를 한 번의 LLM 호출로
처리해 호출 수를 아낀다.
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol

from app.agents.events.photo.image_source import (
    NullPhotoImageSource,
    PhotoImageSource,
)
from app.agents.parsing import SupportsComplete, default_llm
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.llm import ImageInput
from app.core.logging import get_logger
from app.core.execution_context import ExecutionStage
from app.schemas import PhotoItem

logger = get_logger(__name__)

_DESCRIBE_PROMPT = (Path(__file__).parent / "describe_prompt.md").read_text(
    encoding="utf-8"
)


class SupportsVision(Protocol):
    """이미지 입력(vision) 을 지원하는 LLM 최소 인터페이스."""

    def complete_with_images(
        self, prompt: str, images: list[ImageInput], *, system: str | None = ..., **kwargs
    ) -> str: ...


def needs_description(photo: PhotoItem) -> bool:
    """description 이 비어 있어 생성이 필요한 사진인지 판단한다."""

    return not (photo.description or "").strip()


def parse_descriptions(text: str, valid_raw_ids: set[str]) -> dict[str, str]:
    """사진 설명 응답을 ``{rawId: description}``으로 파싱한다.

    JSON 을 찾지 못하거나 파싱에 실패하면 빈 dict 를 반환한다(예외를 던지지 않는다).
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        report_error(
            logger,
            ErrorCode.STRUCTURED_OUTPUT_INVALID,
            "사진 description 응답에서 JSON 객체를 찾지 못했습니다",
            stage=ExecutionStage.EVENT_AGENT,
            context={"parser": "photo_description"},
        )
        return {}
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        report_error(
            logger,
            ErrorCode.STRUCTURED_OUTPUT_INVALID,
            "사진 description JSON 파싱에 실패했습니다",
            exc=exc,
            stage=ExecutionStage.EVENT_AGENT,
            context={"parser": "photo_description"},
        )
        return {}

    result: dict[str, str] = {}
    for item in payload.get("descriptions") or []:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("rawId")
        if not isinstance(raw_id, str):
            continue
        description = (item.get("description") or "").strip()
        if raw_id in valid_raw_ids and description:
            result[raw_id] = description
    return result


class PhotoDescriber(ABC):
    """description 이 없는 사진에 설명을 채우는 생성기 인터페이스."""

    @abstractmethod
    def describe(self, photos: list[PhotoItem]) -> dict[str, str]:
        """description 이 필요한 사진의 ``{rawId: description}``을 반환한다.

        이미 description 이 있는 사진은 포함하지 않는다. 생성 실패 시 해당 사진을
        결과에서 빼면 되고, 예외를 던지지 않는다.
        """


class LLMPhotoDescriber(PhotoDescriber):
    """메타데이터를 배치로 넣어 description 을 생성하는 기본 구현.

    아직 실제 이미지(S3)에 접근하지 않으므로, 파일명·촬영 시각·GPS 등 가용
    메타데이터만으로 설명을 신중히 추정한다. 실제 이미지를 보지 못하는 한계 때문에
    설명은 확정적이지 않으며, 이후 event 추론이 이를 감안한다.
    """

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def describe(self, photos: list[PhotoItem]) -> dict[str, str]:
        targets = [photo for photo in photos if needs_description(photo)]
        if not targets:
            return {}  # 채울 사진이 없으면 LLM 을 호출하지 않는다.

        prompt = _metadata_prompt(targets)
        text = self.llm.complete(prompt, system=_DESCRIBE_PROMPT, temperature=0.2)
        return parse_descriptions(text, {photo.raw_id for photo in targets})


def _metadata_prompt(photos: list[PhotoItem]) -> str:
    """이미지 없이 메타데이터만으로 설명을 요청하는 프롬프트."""

    rows = [
        {
            "rawId": photo.raw_id,
            "filename": photo.filename,
            "takenAt": photo.taken_at,
            "lat": photo.latitude,
            "lon": photo.longitude,
        }
        for photo in photos
    ]
    return (
        "아래 사진들의 description 을 각각 생성하세요. 실제 이미지는 제공되지 않으니 "
        "메타데이터로만 신중히 추정하고, 보이지 않는 구체 사물을 지어내지 말며 "
        "확실하지 않으면 일반적으로 표현하세요.\n\n"
        f"{json.dumps(rows, ensure_ascii=False, indent=2)}\n\n"
        '반드시 {"descriptions": [{"rawId": "<입력 rawId>", "description": "..."}]} 형식의 '
        "JSON 만 출력하세요."
    )


class VisionPhotoDescriber(PhotoDescriber):
    """이미지 소스에서 실제 사진을 불러와 vision 모델로 description 을 만드는 describer.

    S3(또는 로컬 파일) 등 `PhotoImageSource` 에서 이미지를 조회해 **배치 1회**
    vision 호출로 처리한다. 이미지를 구하지 못한 사진은 메타데이터 기반
    describer(`fallback`)로 대체해, 이미지가 일부만 있어도 최대한 채운다.

    이미지 소스가 `NullPhotoImageSource`(기본)면 모든 사진이 fallback 으로 가므로,
    S3 가 연결되기 전에는 `LLMPhotoDescriber` 와 동일하게 동작한다.
    """

    def __init__(
        self,
        image_source: PhotoImageSource | None = None,
        llm: SupportsVision | None = None,
        fallback: PhotoDescriber | None = None,
    ) -> None:
        self._image_source = image_source or NullPhotoImageSource()
        self._llm = llm
        self._fallback = fallback

    @property
    def llm(self) -> SupportsVision:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    @property
    def fallback(self) -> PhotoDescriber:
        if self._fallback is None:
            self._fallback = LLMPhotoDescriber(self._llm)
        return self._fallback

    def describe(self, photos: list[PhotoItem]) -> dict[str, str]:
        targets = [photo for photo in photos if needs_description(photo)]
        if not targets:
            return {}

        with_image: list[PhotoItem] = []
        images: list[ImageInput] = []
        without_image: list[PhotoItem] = []
        for photo in targets:
            image = self._image_source.load(photo)
            if image is None:
                without_image.append(photo)
            else:
                with_image.append(photo)
                images.append(image)

        result: dict[str, str] = {}
        if with_image:
            result.update(self._describe_with_vision(with_image, images))
        if without_image:
            # 이미지를 못 구한 사진은 메타데이터 기반으로 채운다.
            result.update(self.fallback.describe(without_image))
        return result

    def _describe_with_vision(
        self, photos: list[PhotoItem], images: list[ImageInput]
    ) -> dict[str, str]:
        prompt = _vision_prompt(photos)
        text = self.llm.complete_with_images(
            prompt, images, system=_DESCRIBE_PROMPT, temperature=0.2
        )
        return parse_descriptions(text, {photo.raw_id for photo in photos})


def _vision_prompt(photos: list[PhotoItem]) -> str:
    """첨부 이미지 순서와 rawId를 함께 알려 vision 설명을 요청하는 프롬프트."""

    rows = [
        {"rawId": photo.raw_id, "filename": photo.filename, "takenAt": photo.taken_at}
        for photo in photos
    ]
    return (
        "아래 목록의 순서대로 사진 이미지가 첨부됩니다. 각 이미지를 보고 해당 rawId의 "
        "description 을 생성하세요.\n\n"
        f"{json.dumps(rows, ensure_ascii=False, indent=2)}\n\n"
        '반드시 {"descriptions": [{"rawId": "<입력 rawId>", "description": "..."}]} 형식의 '
        "JSON 만 출력하세요."
    )
