"""테스트용 입력 요청 빌더.

source type 별로 최소한의 `TimelineDraftRequest` 를 손쉽게 만들도록 돕는다.
필요한 도메인만 채워 각 Event Agent 를 독립적으로 검증할 수 있다.
"""

from app.schemas import (
    ActiveCaloriesData,
    CalendarData,
    CalendarEvent,
    CollectionMode,
    DistanceData,
    HealthData,
    HeartRateData,
    HeartRateSample,
    LocationData,
    LocationRoute,
    LocationStay,
    NotificationData,
    NotificationItem,
    PhotoItem,
    SleepData,
    StepsData,
    TimelineDraftRequest,
    TimeWindow,
    TotalCaloriesData,
)

# 하루 기준 epoch milliseconds (2026-06-20 00:00 UTC 근처의 임의 고정값).
DAY_START = 1_750_000_000_000
HOUR = 3_600_000


def make_request(**overrides) -> TimelineDraftRequest:
    """봉투 필수 필드를 채운 요청을 만든다. 도메인은 override 로 주입한다."""

    defaults = dict(
        transaction_id="tx-test",
        date="2026-06-20",
        mode=CollectionMode.FULL_DAY,
        window=TimeWindow(start=DAY_START, end=DAY_START + 24 * HOUR),
        generated_at=DAY_START,
    )
    defaults.update(overrides)
    return TimelineDraftRequest(**defaults)


def stay(source_id, lat, lon, start, end) -> LocationStay:
    return LocationStay(
        source_id=source_id,
        source="live",
        lat=lat,
        lon=lon,
        start_time=start,
        end_time=end,
        duration_sec=(end - start) // 1000,
    )


def route(source_id, start, end, distance=1000.0) -> LocationRoute:
    return LocationRoute(
        source_id=source_id,
        source="live",
        start_time=start,
        end_time=end,
        duration_sec=(end - start) // 1000,
        distance_meters=distance,
    )


def calendar_event(source_id, title, start, end) -> CalendarEvent:
    return CalendarEvent(
        source_id=source_id,
        title=title,
        start_time=start,
        end_time=end,
    )


def photo(source_id, at, mime="image/jpeg", lat=None, lon=None) -> PhotoItem:
    return PhotoItem(
        source_id=source_id,
        id=source_id,
        uri=f"file:///{source_id}.jpg",
        date_taken=at,
        lat=lat,
        lon=lon,
        width=1920,
        height=1080,
        mime_type=mime,
    )


def notification(source_id, app_name, title, post) -> NotificationItem:
    return NotificationItem(
        source_id=source_id,
        package_name=f"com.{app_name.lower()}",
        app_name=app_name,
        title=title,
        text="본문",
        post_time=post,
        collected_at=post,
    )


def sleep(source_id, start, end) -> SleepData:
    return SleepData(
        source_id=source_id,
        start_time=start,
        end_time=end,
        duration_minutes=(end - start) // 60_000,
    )


def heart_rate(source_id, sample_times) -> HeartRateData:
    return HeartRateData(
        source_id=source_id,
        samples=[HeartRateSample(time=t, bpm=70) for t in sample_times],
    )


def steps(source_id, count) -> StepsData:
    return StepsData(source_id=source_id, count=count)


def total_calories(source_id, kcal) -> TotalCaloriesData:
    return TotalCaloriesData(source_id=source_id, kcal=kcal)


def active_calories(source_id, kcal) -> ActiveCaloriesData:
    return ActiveCaloriesData(source_id=source_id, kcal=kcal)


def distance(source_id, meters) -> DistanceData:
    return DistanceData(source_id=source_id, meters=meters)


def location_data(stays=(), routes=()) -> LocationData:
    return LocationData(stays=list(stays), routes=list(routes))


def notification_data(active=(), collected=()) -> NotificationData:
    return NotificationData(active=list(active), collected=list(collected))


def calendar_data(events=()) -> CalendarData:
    return CalendarData(events=list(events))


def health_data(**kwargs) -> HealthData:
    return HealthData(**kwargs)
