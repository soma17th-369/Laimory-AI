"""테스트용 입력 빌더.

- `make_request`: 정규화된 `TimelineDraftRequest` 를 손쉽게 만든다(Agent/파이프라인
  단위 테스트용). 도메인 항목은 override 로 주입한다.
- `make_snapshot` / `*_source_item`: 수집 원본(`CollectedSnapshot`)을 만든다
  (normalizer/저장소/엔드포인트 e2e 테스트용).

시각은 새 계약대로 ISO 문자열을 사용한다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5

from app.schemas import (
    CalendarItem,
    CollectedSnapshot,
    CollectedSourceItem,
    HealthItem,
    HealthMetric,
    ItemType,
    MovementItem,
    NotificationItem,
    PhotoItem,
    StayItem,
    TimelineDraftRequest,
    TimelineWindow,
    TimeWindow,
)

DATE = "2026-06-20"
WINDOW_START = "2026-06-20T00:00:00"
WINDOW_END = "2026-06-21T00:00:00"


def fixture_raw_id(label: str) -> str:
    """테스트 라벨에서 재현 가능한 UUID rawId를 만든다."""

    try:
        return str(UUID(label))
    except ValueError:
        pass
    return str(uuid5(NAMESPACE_URL, f"laimory-test:{label}"))


# --- 정규화된 요청(TimelineDraftRequest) 빌더 ---------------------------------


def make_request(**overrides) -> TimelineDraftRequest:
    """봉투 필수 필드를 채운 요청을 만든다. 도메인 리스트는 override 로 주입한다."""

    defaults = dict(
        task_id="task-test",
        date=DATE,
        timezone="Asia/Seoul",
        window=TimeWindow(start=WINDOW_START, end=WINDOW_END),
    )
    defaults.update(overrides)
    return TimelineDraftRequest(**defaults)


def stay_item(
    item_id,
    lat=37.5,
    lon=127.0,
    start=WINDOW_START,
    end=WINDOW_END,
    raw_id=None,
    place=None,
    address="서울특별시 어딘가",
    places=None,
) -> StayItem:
    return StayItem(
        raw_id=fixture_raw_id(raw_id or f"stay-{item_id}"),
        start_at=start,
        end_at=end,
        latitude=lat,
        longitude=lon,
        place=place,
        address=address,
        places=["장소명"] if places is None else places,
        duration_text="1시간",
    )


def movement_item(
    item_id,
    start=WINDOW_START,
    end=WINDOW_END,
    distance=1000.0,
    raw_id=None,
    transports=None,
) -> MovementItem:
    return MovementItem(
        raw_id=fixture_raw_id(raw_id or f"movement-{item_id}"),
        start_at=start,
        end_at=end,
        distance_meters=distance,
        transports=["WALKING"] if transports is None else list(transports),
    )


def calendar_item(
    item_id, title, start=WINDOW_START, end=WINDOW_END, raw_id=None, location_text=None
) -> CalendarItem:
    return CalendarItem(
        raw_id=fixture_raw_id(raw_id or f"calendar-{item_id}"),
        start_at=start,
        end_at=end,
        title=title,
        location_text=location_text,
    )


def notification_item(item_id, app_name, title, posted=WINDOW_START, raw_id=None) -> NotificationItem:
    return NotificationItem(
        raw_id=fixture_raw_id(raw_id or f"notification-{item_id}"),
        posted_at=posted,
        app_name=app_name,
        title=title,
        text="본문",
    )


def photo_item(
    item_id,
    taken=WINDOW_START,
    lat=None,
    lon=None,
    raw_id=None,
    photo_url=None,
    places=None,
    address=None,
) -> PhotoItem:
    return PhotoItem(
        raw_id=fixture_raw_id(raw_id or f"photo-{item_id}"),
        taken_at=taken,
        latitude=lat,
        longitude=lon,
        places=places or [],
        address=address,
        description="사진 설명",
        photo_url=photo_url,
    )


def steps_item(item_id, count, start=WINDOW_START, end=WINDOW_END, raw_id=None) -> HealthItem:
    return HealthItem(
        raw_id=fixture_raw_id(raw_id or f"steps-{item_id}"),
        metric=HealthMetric.STEPS,
        start_at=start,
        end_at=end,
        value=count,
    )


def sleep_item(item_id, start, end, minutes, raw_id=None) -> HealthItem:
    return HealthItem(
        raw_id=fixture_raw_id(raw_id or f"sleep-{item_id}"),
        metric=HealthMetric.SLEEP,
        start_at=start,
        end_at=end,
        duration_minutes=minutes,
    )


# --- 수집 원본(CollectedSnapshot) 빌더 ----------------------------------------


def source_item(
    item_id, item_type, payload, start=WINDOW_START, end=None, raw_id=None
) -> CollectedSourceItem:
    return CollectedSourceItem(
        raw_id=fixture_raw_id(raw_id or f"source-{item_id}"),
        item_type=item_type,
        start_at=start,
        end_at=end,
        payload=payload,
    )


def make_snapshot(task_id="task-test", source_items=None, **overrides) -> CollectedSnapshot:
    """수집 원본 스냅샷을 만든다. sourceItems 는 리스트로 주입한다."""

    defaults = dict(
        task_id=task_id,
        record_date="2026-06-20T22:00:00",
        record_time_zone="Asia/Seoul",
        timeline_window=TimelineWindow(start_time="20260620T000000", end_time="20260621T000000"),
        source_items=list(source_items or []),
    )
    defaults.update(overrides)
    return CollectedSnapshot(**defaults)


def default_source_items() -> list[CollectedSourceItem]:
    """도메인마다 한 건씩 담긴 대표 sourceItems."""

    return [
        source_item(
            101,
            ItemType.STAY,
            {"latitude": 37.5, "longitude": 127.0, "address": "주소", "places": ["장소"], "durationText": "1시간"},
            end=WINDOW_END,
        ),
        source_item(
            102,
            ItemType.MOVEMENT,
            {
                "start": {"latitude": 37.5, "longitude": 127.0},
                "end": {"latitude": 37.6, "longitude": 127.1},
                "distanceMeters": 5000.0,
                "transports": ["IN_VEHICLE"],
            },
            end=WINDOW_END,
        ),
        source_item(
            103,
            ItemType.CALENDAR,
            {"title": "회의", "description": "", "locationText": "회의실", "allDay": False},
            end=WINDOW_END,
        ),
        source_item(104, ItemType.HEALTH, {"metric": "STEPS", "value": 10145}, end=WINDOW_END),
        source_item(
            105,
            ItemType.HEALTH,
            {"metric": "SLEEP", "durationMinutes": 210},
            start="2026-06-20T04:00:00",
            end="2026-06-20T07:30:00",
        ),
        source_item(
            106,
            ItemType.NOTIFICATION,
            {"appName": "카카오톡", "title": "제목", "text": "본문"},
        ),
        source_item(
            107,
            ItemType.PHOTO,
            {
                # filename/clientPhotoUri 는 App Server 가 아직 보내지만 PhotoItem 에
                # 필드가 없어 무시된다(#52). 무시가 실패로 번지지 않는지 함께 본다.
                "filename": "p.jpg",
                "clientPhotoUri": "content://p",
                "photoUrl": "https://images.example.com/p.jpg",
                "description": "설명",
            },
        ),
    ]
