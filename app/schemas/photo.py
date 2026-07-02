"""사진 source item 스키마.

사진 메타데이터(ID/URI, 촬영 시각, GPS 좌표, 이미지 크기, MIME 타입)를
정의한다. GPS 좌표는 사진에 위치 정보가 없을 수 있어 선택 값이다.
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import Latitude, Longitude, SourceItem, SourceType, TimestampMs


class PhotoItem(SourceItem):
    """사진 한 장의 메타데이터."""

    source_type: Literal[SourceType.PHOTO] = Field(SourceType.PHOTO, alias="sourceType")
    id: str
    uri: str
    date_taken: TimestampMs = Field(alias="dateTaken")
    lat: Latitude | None = None
    lon: Longitude | None = None
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: str = Field(alias="mimeType")
