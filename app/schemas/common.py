"""타임라인 입력 공통 스키마.

수집 데이터를 도메인별로 분리한 뒤 각 도메인 모델이 공유하는 공통 base
모델(`CamelModel`)과 재사용 타입(좌표·장소)을 정의한다.

시각은 수집 원본이 ISO 8601 문자열(예: `"2026-06-30T10:00:00"`)로 주므로
epoch milliseconds 대신 문자열을 그대로 사용한다. 각 도메인 항목은 원본
`sourceItems` 의 `startAt`/`endAt` 를 문자열로 물려받는다.
"""

from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


class CamelModel(BaseModel):
    """프로젝트 공통 base 모델.

    요청 JSON 은 camelCase(예: `startAt`, `itemType`)로 들어오므로 alias 로
    매핑하고, 파이썬 코드에서는 snake_case 필드명으로도 값을 채울 수 있도록
    `populate_by_name` 을 켠다.
    """

    model_config = ConfigDict(populate_by_name=True)


# 위경도 좌표. WGS84 유효 범위로 제한한다. NaN·무한대는 범위 밖과 같다 — App Server 는 좌표 하나가
# 어긋나도 결과 batch 전체를 400 으로 거절한다.
Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]


def _normalize_raw_id(value: str) -> str:
    """rawId를 표준 UUID 문자열로 검증·정규화한다."""

    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("rawId must be a valid UUID") from exc


RawId = Annotated[str, AfterValidator(_normalize_raw_id)]


class PlacePoint(CamelModel):
    """입력의 주변 장소 하나와 그 좌표(입력 조회 v2, App Server 명세 2026-10-30).

    맨 앞 건물명의 좌표는 장소 좌표가 아니라 그 항목의 관측 좌표다(카카오가 건물 좌표를 주지 않는다).
    """

    name: str
    latitude: Latitude
    longitude: Longitude


def split_place_objects(data: object) -> object:
    """입력 조회 v2 의 `places`(`[{name, latitude, longitude}]`)를 이름과 좌표로 가른다.

    `places` 는 이름 목록 그대로 둔다 — 스키마는 추가만 하고, 이름을 읽는 코드와 프롬프트는 입력 버전을
    몰라도 된다. 좌표는 새 필드 `placePoints` 에 담는다. 두 형태가 섞여 와도 받는다. 좌표가 어긋난
    원소는 이름만 남긴다 — 좌표 하나 때문에 입력 항목을 버리면(1102) 하루치가 실패한다.
    """

    if not isinstance(data, dict):
        return data
    places = data.get("places")
    if not isinstance(places, list) or not any(isinstance(item, dict) for item in places):
        return data

    names: list[object] = []
    points: list[PlacePoint] = []
    for item in places:
        if not isinstance(item, dict):
            names.append(item)
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        names.append(name)
        try:
            points.append(PlacePoint.model_validate(item))
        except ValidationError:
            continue
    return {**data, "places": names, "placePoints": points}


class GeoPlace(CamelModel):
    """좌표 + 주소 + 장소명을 갖는 한 지점.

    체류(`StayItem`)/이동(`MovementItem`)의 출발·도착 지점을 표현한다.
    수집 단계에서 일부 값이 비어 있을 수 있어 전부 선택 값으로 둔다.
    """

    latitude: Latitude | None = None
    longitude: Longitude | None = None
    place: str | None = None
    address: str | None = None
    places: list[str] = Field(default_factory=list)
    place_points: list[PlacePoint] = Field(default_factory=list, alias="placePoints")

    _split_places = model_validator(mode="before")(split_place_objects)
