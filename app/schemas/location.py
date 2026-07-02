"""위치 source item 스키마.

특정 장소에 머문 기록(`stays`)과 장소 간 이동 기록(`routes`)을 정의한다.
좌표는 `lat`/`lon`, 시각은 Unix epoch milliseconds 를 사용한다.
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import (
    CamelModel,
    GeoPoint,
    Latitude,
    Longitude,
    SourceItem,
    SourceType,
    TimeRange,
)


class LocationStay(SourceItem, TimeRange):
    """특정 장소에 머문 기록."""

    source_type: Literal[SourceType.LOCATION_STAY] = Field(
        SourceType.LOCATION_STAY, alias="sourceType"
    )
    source: str = Field(description="수집 출처 (예: live)")
    lat: Latitude
    lon: Longitude
    duration_sec: int = Field(alias="durationSec", ge=0)


class LocationRoute(SourceItem, TimeRange):
    """장소 간 이동 기록."""

    source_type: Literal[SourceType.LOCATION_ROUTE] = Field(
        SourceType.LOCATION_ROUTE, alias="sourceType"
    )
    source: str = Field(description="수집 출처 (예: live)")
    duration_sec: int = Field(alias="durationSec", ge=0)
    distance_meters: float = Field(alias="distanceMeters", ge=0)
    points: list[GeoPoint] = Field(default_factory=list)


class LocationData(CamelModel):
    """위치 도메인 컨테이너."""

    stays: list[LocationStay] = Field(default_factory=list)
    routes: list[LocationRoute] = Field(default_factory=list)
