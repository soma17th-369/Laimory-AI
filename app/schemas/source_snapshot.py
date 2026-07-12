"""수집 스냅샷(원본) 입력 계약.

App Server 가 하루치로 수집한 데이터를 DB 에 적재해 두고, AI 서버에는
`taskId` 만 전달한다. AI 서버는 `taskId` 로 이 `CollectedSnapshot` 을 읽어온다.

원본은 도메인별로 나뉘어 있지 않고, 모든 항목이 `sourceItems` 라는 **평평한
리스트**에 `itemType` 과 함께 담겨 온다. 각 항목의 `payload` 는 `itemType`
마다 형태가 달라 여기서는 그대로 `dict` 로 받고, 도메인 모델로의 파싱과
분리는 `app/services/normalizer.py` 가 담당한다.
"""

from enum import Enum
from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.user_memory import UserMemory


class ItemType(str, Enum):
    """수집 항목 종류.

    `payload` 의 형태를 결정하는 구분자이며, normalizer 가 이 값으로 도메인을
    나눈다. 새 수집 종류가 생기면 여기에 값을 추가한다.
    """

    STAY = "STAY"
    MOVEMENT = "MOVEMENT"
    CALENDAR = "CALENDAR"
    HEALTH = "HEALTH"
    NOTIFICATION = "NOTIFICATION"
    PHOTO = "PHOTO"


class HealthMetric(str, Enum):
    """HEALTH 항목의 세부 지표.

    같은 `itemType=HEALTH` 안에서 `payload.metric` 으로 걸음 수/수면 등을
    구분한다. 지표가 늘어나면 여기에 추가한다.
    """

    STEPS = "STEPS"
    SLEEP = "SLEEP"


class CollectedSourceItem(CamelModel):
    """수집된 단일 항목.

    `payload` 는 `itemType` 별로 형태가 달라 원본 dict 로 받는다. 실제 도메인
    모델 검증은 normalizer 에서 수행한다.
    """

    id: int
    raw_id: str | None = Field(default=None, alias="rawId")
    item_type: ItemType = Field(alias="itemType")
    start_at: str = Field(alias="startAt")
    end_at: str | None = Field(default=None, alias="endAt")
    payload: dict[str, Any] = Field(default_factory=dict)


class TimelineWindow(CamelModel):
    """타임라인 생성 대상 시간 창.

    `"YYYYMMDDTHHmmss"` 같은 압축 ISO 문자열로 오며, 포맷이 유동적일 수 있어
    엄격 검증 없이 문자열로 보관한다.
    """

    start_time: str = Field(alias="startTime")
    end_time: str = Field(alias="endTime")


class CollectedSnapshot(CamelModel):
    """`taskId` 로 DB 에서 읽어오는 하루치 수집 원본."""

    task_id: str = Field(alias="taskId", min_length=1)
    record_date: str = Field(alias="recordDate")
    record_time_zone: str = Field(default="Asia/Seoul", alias="recordTimeZone")
    user_memory: UserMemory | None = Field(default=None, alias="userMemory")
    timeline_window: TimelineWindow | None = Field(default=None, alias="timelineWindow")
    source_items: list[CollectedSourceItem] = Field(
        default_factory=list, alias="sourceItems"
    )
