"""사진 description 생성기 (PhotoEventAgent 의 describe 단계).

수집 입력의 사진 `description` 은 보통 `null` 로 온다. PhotoEventAgent 가 event
를 추론하기 전에, description 이 비어 있는 사진들의 설명을 여기서 채운다.

description 은 **실제 사진**을 vision 모델로 보고 만들어야 정확하다. 그래서 경로가
두 단계뿐이다(#59).

1. 모든 사진은 `VisionPhotoDescriber` 를 거친다. `PhotoImageSource`(운영에서는 App
   Server 가 준 `photoUrl`)에서 이미지를 내려받아 `describe_vision_prompt.md` 와 함께
   vision 호출에 싣는다.
2. 이미지를 구하지 못했거나 vision 응답에서 빠진 사진만 `LLMPhotoDescriber` 가
   `describe_prompt.md` 로 채운다. 프롬프트 버전과 무관하게 같은 순서다.

호출 단위는 **배치 1회**다. description 이 필요한 사진 전부를 한 번의 LLM 호출로
처리해 호출 수를 아낀다. 그래서 이미지 총량 상한이 중요하다 — 한 요청에 실리는
bytes 가 그대로 provider 요청 크기가 된다(`image_source.load_images` 가 제한한다).
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Protocol

from app.agents.events.photo.image_source import (
    NullPhotoImageSource,
    PhotoImageSource,
    default_photo_image_source,
    load_images,
)
from app.agents.parsing import SupportsComplete, default_llm
from app.agents.prompt_loader import load_prompt
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.langfuse_tracing import trace_observation, update_observation
from app.core.llm import ImageInput
from app.core.logging import get_logger
from app.core.execution_context import ExecutionStage
from app.schemas import PhotoItem, StayItem
from app.services.validator import parse_datetime

logger = get_logger(__name__)

_VISION_DESCRIBE_PROMPT = load_prompt(__file__, "describe_vision_prompt.md")

#: 이미지를 구하지 못한 사진의 fallback 프롬프트. 프롬프트 버전과 무관하게 항상 쓴다(#59).
#:
#: v2 는 한때 이 자리를 코드가 만드는 `MetadataPhotoDescriber` 로 바꿨었다(#56 §12).
#: 이미지를 못 본 채 장면을 지어내는 걸 막으려는 의도였는데, 정작 vision 경로가 설정
#: 때문에 한 번도 돌지 않아 **모든** 사진이 그 고정 문장을 받고 있었다. vision 을 기본으로
#: 되돌리면서 fallback 도 다시 프롬프트 기반으로 통일했다.
_DESCRIBE_PROMPT = load_prompt(__file__, "describe_prompt.md")


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
    """메타데이터를 배치로 넣어 LLM 이 description 을 추정하는 fallback (#59).

    이미지를 보지 못한 채 촬영 시각·좌표만으로 추정하므로 설명이 확정적이지 않다.
    그래서 **이미지를 구하지 못한 사진에만** 쓴다. 정상 경로는 언제나
    `VisionPhotoDescriber` 의 vision 호출이다.

    `stays` 를 주면 촬영 시각을 덮는 체류의 장소명을 프롬프트에 함께 싣는다.
    `describe_prompt.md` 가 "입력에 장소명이나 주소가 함께 제공된 경우에만 해당 명칭을
    사용" 하도록 쓰여 있어, 이 값이 있어야 그 문장이 의미를 갖는다.
    """

    def __init__(
        self,
        llm: SupportsComplete | None = None,
        stays: list[StayItem] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._llm = llm
        self._stays = list(stays or [])
        self._tz = tz or timezone(timedelta(hours=9))

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def describe(self, photos: list[PhotoItem]) -> dict[str, str]:
        targets = [photo for photo in photos if needs_description(photo)]
        if not targets:
            return {}  # 채울 사진이 없으면 LLM 을 호출하지 않는다.

        prompt = _metadata_prompt(targets, self._stays, self._tz)
        text = self.llm.complete(prompt, system=_DESCRIBE_PROMPT, temperature=0.2)
        return parse_descriptions(text, {photo.raw_id for photo in targets})


def _place_at(
    stays: list[StayItem], taken_at: datetime | None, tz: tzinfo
) -> str | None:
    """촬영 시각을 덮는 체류의 장소명. 없으면 None."""

    if taken_at is None:
        return None
    for stay in stays:
        start = parse_datetime(stay.start_at, tz)
        end = parse_datetime(stay.end_at, tz) if stay.end_at else start
        if start is None or end is None:
            continue
        if start <= taken_at <= end:
            return stay.place or stay.address
    return None


def _metadata_prompt(
    photos: list[PhotoItem],
    stays: list[StayItem] | None = None,
    tz: tzinfo | None = None,
) -> str:
    """이미지 없이 메타데이터만으로 설명을 요청하는 프롬프트.

    `place` 는 근거가 있을 때만 넣는다. 값을 만들어 채우면 LLM 이 그걸 사실로 읽는다.
    """

    stays = list(stays or [])
    tz = tz or timezone(timedelta(hours=9))
    rows = []
    for photo in photos:
        row: dict[str, object] = {
            "rawId": photo.raw_id,
            "takenAt": photo.taken_at,
            "lat": photo.latitude,
            "lon": photo.longitude,
        }
        place = _place_at(stays, parse_datetime(photo.taken_at, tz), tz)
        if place:
            row["place"] = place
        rows.append(row)
    return (
        "아래 사진들의 description 을 각각 생성하세요. 실제 이미지는 제공되지 않으니 "
        "메타데이터로만 신중히 추정하고, 보이지 않는 구체 사물을 지어내지 말며 "
        "확실하지 않으면 일반적으로 표현하세요.\n\n"
        f"{json.dumps(rows, ensure_ascii=False, indent=2)}\n\n"
        '반드시 {"descriptions": [{"rawId": "<입력 rawId>", "description": "..."}]} 형식의 '
        "JSON 만 출력하세요."
    )


class MetadataPhotoDescriber(PhotoDescriber):
    """메타데이터만으로 description 을 **코드가** 만드는 구현 (#56 §12).

    LLM 없이 아는 사실(언제 찍혔는가, 좌표가 있는가, 그 시각 사용자가 어디에 있었는가)만
    문장으로 옮긴다. 지어낼 여지가 없는 대신 사진마다 거의 같은 문장이 나온다.

    **기본 경로에서는 더 이상 쓰지 않는다(#59).** 한때 v2 의 fallback 이었는데, 정작
    vision 이 설정 때문에 한 번도 돌지 않아 모든 사진이 이 고정 문장을 받고 있었다.
    지금 fallback 은 `LLMPhotoDescriber` 다. 이 클래스는 LLM 호출 없이 결정론적 설명이
    필요할 때(테스트·오프라인 검증) 주입해서 쓴다.

    `stays` 를 주면 촬영 시각을 덮는 체류의 장소명을 함께 넣는다.
    """

    def __init__(self, stays: list[StayItem] | None = None, tz: tzinfo | None = None) -> None:
        self._stays = list(stays or [])
        self._tz = tz or timezone(timedelta(hours=9))

    def describe(self, photos: list[PhotoItem]) -> dict[str, str]:
        targets = [photo for photo in photos if needs_description(photo)]
        return {photo.raw_id: self._describe_one(photo) for photo in targets}

    def _describe_one(self, photo: PhotoItem) -> str:
        taken_at = parse_datetime(photo.taken_at, self._tz)
        parts: list[str] = []

        if taken_at is not None:
            parts.append(f"{taken_at.strftime('%H:%M')}에 찍은 사진")
        else:
            parts.append("촬영 시각을 알 수 없는 사진")

        place = _place_at(self._stays, taken_at, self._tz)
        if place:
            parts.append(f"{place}에서 촬영된 것으로 보인다")
        elif photo.latitude is not None and photo.longitude is not None:
            parts.append("촬영 위치 좌표가 함께 기록돼 있다")

        parts.append("이미지를 확인하지 못해 무엇이 담겼는지는 알 수 없다")
        return ". ".join(parts) + "."


class VisionPhotoDescriber(PhotoDescriber):
    """이미지 소스에서 실제 사진을 불러와 vision 모델로 description 을 만드는 describer.

    `PhotoImageSource`(운영에서는 `photoUrl` 다운로드)에서 이미지를 모아 **배치 1회**
    vision 호출로 처리한다. 이미지를 구하지 못했거나 vision 응답에서 빠진 사진만
    `fallback`(기본 `LLMPhotoDescriber`)으로 대체해, 이미지가 일부만 있어도 최대한 채운다.

    이미지 소스 기본값은 `default_photo_image_source()` 라 별도 설정 없이 곧바로
    `photoUrl` 을 내려받는다(#59). `NullPhotoImageSource` 를 명시로 주입하면 모든 사진이
    fallback 으로 가는데, 이건 테스트에서 다운로드를 끌 때 쓰는 경로다.

    `stays` 는 fallback 이 촬영 시각의 장소를 프롬프트에 실을 때 쓴다.
    """

    def __init__(
        self,
        image_source: PhotoImageSource | None = None,
        llm: SupportsVision | None = None,
        fallback: PhotoDescriber | None = None,
        stays: list[StayItem] | None = None,
    ) -> None:
        self._image_source = image_source or default_photo_image_source()
        self._llm = llm
        self._fallback = fallback
        self._stays = list(stays or [])

    @property
    def llm(self) -> SupportsVision:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    @property
    def fallback(self) -> PhotoDescriber:
        """이미지를 못 구한 사진을 채우는 2단계. 프롬프트 버전과 무관하다(#59)."""

        if self._fallback is None:
            self._fallback = LLMPhotoDescriber(self._llm, self._stays)
        return self._fallback

    def describe(self, photos: list[PhotoItem]) -> dict[str, str]:
        targets = [photo for photo in photos if needs_description(photo)]
        if not targets:
            return {}

        # 이미지 소스를 명시로 꺼 둔 경우다(테스트). 다운로드 실패가 아니므로
        # thread pool·WARNING 관측 없이 바로 fallback 한다.
        if isinstance(self._image_source, NullPhotoImageSource):
            return self.fallback.describe(targets)

        with_image: list[PhotoItem] = []
        images: list[ImageInput] = []
        without_image: list[PhotoItem] = []

        # 다운로드는 별도 관측으로 남긴다. 실패해도 타임라인은 성공하므로, 관측이
        # 없으면 "설명이 왜 메타데이터 수준인지" 를 나중에 알 수 없다.
        with trace_observation(
            "download-photo-images", as_type="span"
        ) as observation:
            outcome = load_images(self._image_source, targets)
            # 개수·크기·소요시간은 본문이 아니라 라벨이므로 metadata 로 넣는다.
            # (input/output 은 기본 차단 allowlist 를 타서 이 값들이 접힌다.)
            update_observation(
                observation,
                metadata=outcome.as_metadata(),
                level="WARNING" if outcome.failed or outcome.skipped else None,
            )

        for photo, image in zip(targets, outcome.images):
            if image is None:
                without_image.append(photo)
            else:
                with_image.append(photo)
                images.append(image)

        result: dict[str, str] = {}
        fallback_targets = list(without_image)
        if with_image:
            try:
                vision_result = self._describe_with_vision(with_image, images)
            except Exception as exc:  # noqa: BLE001 - vision 실패는 metadata로 흡수한다.
                report_error(
                    logger,
                    ErrorCode.LLM_CALL_FAILED,
                    "사진 vision 설명 생성 실패",
                    exc=exc,
                    stage=ExecutionStage.EVENT_AGENT,
                    context={"agent": "photo", "operation": "vision_description"},
                )
                vision_result = {}
            result.update(vision_result)
            # 호출 예외뿐 아니라 불완전/비정상 응답으로 설명이 빠진 사진도 fallback 한다.
            fallback_targets.extend(
                photo for photo in with_image if photo.raw_id not in vision_result
            )
        if fallback_targets:
            # 이미지를 못 구했거나 vision 설명이 실패한 사진은 메타데이터로 채운다.
            result.update(self.fallback.describe(fallback_targets))
        return result

    def _describe_with_vision(
        self, photos: list[PhotoItem], images: list[ImageInput]
    ) -> dict[str, str]:
        prompt = _vision_prompt(photos)
        text = self.llm.complete_with_images(
            prompt, images, system=_VISION_DESCRIBE_PROMPT, temperature=0.2
        )
        return parse_descriptions(text, {photo.raw_id for photo in photos})


def _vision_prompt(photos: list[PhotoItem]) -> str:
    """첨부 이미지 순서와 rawId를 함께 알려 vision 설명을 요청하는 프롬프트."""

    rows = [
        {"rawId": photo.raw_id, "takenAt": photo.taken_at} for photo in photos
    ]
    return (
        "아래 목록의 순서대로 사진 이미지가 첨부됩니다. 각 이미지를 보고 해당 rawId의 "
        "description 을 생성하세요.\n\n"
        f"{json.dumps(rows, ensure_ascii=False, indent=2)}\n\n"
        '반드시 {"descriptions": [{"rawId": "<입력 rawId>", "description": "..."}]} 형식의 '
        "JSON 만 출력하세요."
    )
