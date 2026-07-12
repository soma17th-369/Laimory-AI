"""사진 도메인 항목 스키마.

수집 스냅샷의 `itemType=PHOTO` 를 분리한 뒤의 형태를 정의한다. 사진은 촬영
순간(`taken_at`, 항목의 `startAt`)만 시각으로 갖고, GPS 좌표는 없을 수 있어
선택 값이다. `description` 은 수집 단계에서 미리 생성된 이미지 설명이다.
"""

from pydantic import Field

from app.schemas.common import CamelModel, Latitude, Longitude


class PhotoItem(CamelModel):
    """사진 한 장의 메타데이터 (PHOTO)."""

    id: int
    raw_id: str | None = Field(default=None, alias="rawId")
    taken_at: str = Field(alias="takenAt")
    date_taken: int | None = Field(default=None, alias="dateTaken")
    filename: str | None = Field(default=None, alias="fileName")
    client_photo_uri: str | None = Field(default=None, alias="clientPhotoUri")
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    description: str | None = None
