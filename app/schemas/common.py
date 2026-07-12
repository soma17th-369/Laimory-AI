"""타임라인 입력 공통 스키마.

수집 데이터를 도메인별로 분리한 뒤 각 도메인 모델이 공유하는 공통 base
모델(`CamelModel`)과 재사용 타입(좌표·장소)을 정의한다.

시각은 수집 원본이 ISO 8601 문자열(예: `"2026-06-30T10:00:00"`)로 주므로
epoch milliseconds 대신 문자열을 그대로 사용한다. 각 도메인 항목은 원본
`sourceItems` 의 `startAt`/`endAt` 를 문자열로 물려받는다.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class CamelModel(BaseModel):
    """프로젝트 공통 base 모델.

    요청 JSON 은 camelCase(예: `startAt`, `itemType`)로 들어오므로 alias 로
    매핑하고, 파이썬 코드에서는 snake_case 필드명으로도 값을 채울 수 있도록
    `populate_by_name` 을 켠다.
    """

    model_config = ConfigDict(populate_by_name=True)


# 위경도 좌표. WGS84 유효 범위로 제한한다.
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


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
