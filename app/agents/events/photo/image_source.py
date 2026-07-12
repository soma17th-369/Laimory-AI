"""사진 이미지 소스.

`VisionPhotoDescriber` 가 실제 사진 픽셀을 vision 모델에 넘기려면 이미지 bytes 가
필요하다. 이미지는 앞으로 **S3 에 저장**되어 조회될 예정이며, 아직 연결되지
않았다. 그래서 여기서는 `PhotoImageSource` 인터페이스만 고정하고 구현을 나눈다.

- `S3PhotoImageSource`: 실제 운영 경로(아직 미연결, 스켈레톤).
- `LocalFilePhotoImageSource`: 로컬 디렉터리에서 실제 이미지 파일을 읽는다.
  개발/테스트에서 **실제 이미지 파일**을 넣어 vision 경로를 검증할 때 쓴다.
- `NullPhotoImageSource`: 항상 이미지 없음(None). 이미지 소스가 없을 때 기본값이며,
  이 경우 describer 는 메타데이터 기반으로 대체한다.

디스크 파일명 매칭: 수집 입력의 `photoFile` 은 정규화 단계에서 `clientPhotoUri`
로 매핑되며(예: `000_20260708_172720.jpg`), 실제 저장 파일명과 일치한다. 반면
`fileName`(`filename`, 예: `20260708_172720.jpg`)은 접두어가 없어 실제 파일명과
다를 수 있다. 그래서 로컬 소스는 `clientPhotoUri` → `filename` 순으로 파일을 찾는다.
"""

import mimetypes
from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import settings
from app.core.llm import ImageInput
from app.core.logging import get_logger
from app.schemas import PhotoItem

logger = get_logger(__name__)


def _candidate_names(photo: PhotoItem) -> list[str]:
    """실제 파일명 후보를 우선순위대로 반환한다(`clientPhotoUri` → `filename`).

    URI/경로 형태여도 마지막 경로 세그먼트(basename)만 파일명으로 본다.
    """

    names: list[str] = []
    for value in (photo.client_photo_uri, photo.filename):
        if not value:
            continue
        name = Path(value).name
        if name and name not in names:
            names.append(name)
    return names


class PhotoImageSource(ABC):
    """사진 하나에 대한 실제 이미지 bytes 를 제공하는 인터페이스."""

    @abstractmethod
    def load(self, photo: PhotoItem) -> ImageInput | None:
        """사진의 이미지를 불러온다. 구하지 못하면 None(예외를 던지지 않는다)."""


class NullPhotoImageSource(PhotoImageSource):
    """항상 이미지 없음을 반환하는 기본 소스."""

    def load(self, photo: PhotoItem) -> ImageInput | None:
        return None


class LocalFilePhotoImageSource(PhotoImageSource):
    """로컬 디렉터리에서 실제 이미지 파일을 읽는 소스(개발/테스트용).

    `photo.filename` 을 `base_dir` 아래 파일 이름으로 본다. 파일이 없으면 None.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    def load(self, photo: PhotoItem) -> ImageInput | None:
        names = _candidate_names(photo)
        if not names:
            return None
        for name in names:
            path = self._base_dir / name
            if path.is_file():
                mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
                return ImageInput(data=path.read_bytes(), mime_type=mime)
        logger.warning(
            "이미지 파일을 찾지 못했습니다: base=%s, 후보=%s", self._base_dir, names
        )
        return None


class S3PhotoImageSource(PhotoImageSource):
    """S3 에서 이미지를 조회하는 소스(아직 미연결, 스켈레톤).

    실제 연결 시 `boto3` 등으로 `bucket`/`key` 객체를 받아 `ImageInput` 으로 만든다.
    key 는 `photo.filename` 또는 `clientPhotoUri` 매핑에서 구한다.
    """

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = prefix

    def _key_for(self, photo: PhotoItem) -> str:
        return f"{self._prefix}{photo.filename or photo.id}"

    def load(self, photo: PhotoItem) -> ImageInput | None:
        # TODO: boto3 로 self._bucket/self._key_for(photo) 객체를 받아 ImageInput 반환.
        raise NotImplementedError("S3 이미지 조회는 아직 연결되지 않았습니다.")


def default_photo_image_source() -> PhotoImageSource:
    """설정 기반 기본 이미지 소스를 만든다.

    `settings.photo_image_dir` 이 지정되어 있으면 해당 로컬 디렉터리에서 실제
    이미지를 읽고(`LocalFilePhotoImageSource`), 없으면 이미지 없음(Null)으로 두어
    describer 가 메타데이터 기반으로 대체하게 한다. (S3 연결 시 이 함수만 바꾼다.)
    """

    if settings.photo_image_dir:
        return LocalFilePhotoImageSource(settings.photo_image_dir)
    return NullPhotoImageSource()
