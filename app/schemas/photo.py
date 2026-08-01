"""사진 도메인 항목 스키마.

수집 스냅샷의 `itemType=PHOTO` 를 분리한 뒤의 형태를 정의한다. 사진은 촬영
순간(`taken_at`, 항목의 `startAt`)만 시각으로 갖고, GPS 좌표는 없을 수 있어
선택 값이다. `description` 은 수집 단계에서 미리 생성된 이미지 설명이며,
보통 비어 있어 Photo Agent 가 채운다.

`photo_url` 은 App Server 가 주는 S3 이미지 URL 이다(이슈 #52). Photo Agent 가
이 URL 로 실제 이미지를 내려받아 vision 모델에 넘긴다.

**`exclude=True` 인 이유**: presigned URL 이면 query 에 서명 자격증명이 실린다.
이 모델은 `model_dump()` 로 LLM 프롬프트에 그대로 들어가므로(`agent.py` 의
`_enrich_photo_item`), 필드를 그냥 두면 URL 전체가 프롬프트·Langfuse 로 나간다.
`exclude=True` 는 직렬화 경로 전체에서 값을 빼고, 코드에서는 `photo.photo_url` 로
그대로 읽을 수 있다.

`clientPhotoUri`(`content://media/...`)와 `fileName`(UUID 파일명)은 갖고 있지
않는다. 둘 다 클라이언트/스토리지 내부 식별자라 AI 가 쓸 정보가 없고, 프롬프트에
들어가면 LLM 이 의미를 지어낼 여지만 생긴다. App Server 가 payload 에 실어 보내도
pydantic 이 조용히 무시한다.
"""

from pydantic import Field

from app.schemas.common import CamelModel, Latitude, Longitude, RawId


class PhotoItem(CamelModel):
    """사진 한 장의 메타데이터 (PHOTO)."""

    raw_id: RawId = Field(alias="rawId")
    taken_at: str = Field(alias="takenAt")
    date_taken: int | None = Field(default=None, alias="dateTaken")
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    description: str | None = None
    #: S3 이미지 URL. 직렬화에서 제외한다(위 docstring 참고).
    photo_url: str | None = Field(default=None, alias="photoUrl", exclude=True)
