"""위치/이동 도메인 항목 스키마.

수집 스냅샷의 `itemType=STAY`(특정 장소 체류)와 `itemType=MOVEMENT`
(장소 간 이동)를 분리한 뒤의 형태를 정의한다. 시각은 항목의 `startAt`/`endAt`
문자열을 그대로 물려받는다.
"""

from pydantic import Field

from app.schemas.common import CamelModel, GeoPlace, Latitude, Longitude, RawId


class StayItem(CamelModel):
    """특정 장소에 머문 기록 (STAY)."""

    raw_id: RawId = Field(alias="rawId")
    start_at: str = Field(alias="startAt")
    end_at: str | None = Field(default=None, alias="endAt")
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    place: str | None = None
    address: str | None = None
    places: list[str] = Field(default_factory=list)
    duration_text: str | None = Field(default=None, alias="durationText")


class MovementItem(CamelModel):
    """장소 간 이동 기록 (MOVEMENT)."""

    raw_id: RawId = Field(alias="rawId")
    start_at: str = Field(alias="startAt")
    end_at: str | None = Field(default=None, alias="endAt")
    start: GeoPlace | None = None
    end: GeoPlace | None = None
    duration_text: str | None = Field(default=None, alias="durationText")
    distance_meters: float | None = Field(default=None, alias="distanceMeters", ge=0)
    transports: list[str] = Field(default_factory=list)
