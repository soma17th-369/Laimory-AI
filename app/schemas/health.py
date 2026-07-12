"""건강 도메인 항목 스키마.

수집 스냅샷의 `itemType=HEALTH` 를 분리한 뒤의 형태를 정의한다. 같은 HEALTH
항목이라도 `metric` 으로 걸음 수/수면 등을 구분하며, 지표별로 채워지는 값이
달라(`value` vs `durationMinutes`) 둘 다 선택 값으로 둔다.
"""

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.source_snapshot import HealthMetric


class HealthItem(CamelModel):
    """건강 지표 한 건 (HEALTH).

    - STEPS: `value` 에 걸음 수.
    - SLEEP: `duration_minutes` 에 수면 시간(분), `startAt`/`endAt` 로 구간.
    """

    id: int
    raw_id: str | None = Field(default=None, alias="rawId")
    metric: HealthMetric
    start_at: str = Field(alias="startAt")
    end_at: str | None = Field(default=None, alias="endAt")
    value: float | None = None
    duration_minutes: int | None = Field(default=None, alias="durationMinutes")
