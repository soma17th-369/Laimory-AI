"""수집 스냅샷 분리/정규화(normalizer) 검증.

평평한 sourceItems 가 itemType 기준으로 도메인별로 정확히 분리되고, 각 payload
가 도메인 모델로 파싱되는지 확인한다. 특히 HEALTH 의 metric 분기와 MOVEMENT 의
start/end GeoPlace, 개별 항목 파싱 실패 흡수를 다룬다.
"""

from app.schemas import HealthMetric, ItemType
from app.services.normalizer import normalize, split_source_items
from tests.fixtures.requests import (
    default_source_items,
    make_snapshot,
    source_item,
)


def test_split_groups_by_item_type():
    snapshot = make_snapshot(source_items=default_source_items())
    groups = split_source_items(snapshot)

    assert len(groups[ItemType.STAY]) == 1
    assert len(groups[ItemType.MOVEMENT]) == 1
    assert len(groups[ItemType.CALENDAR]) == 1
    assert len(groups[ItemType.HEALTH]) == 2
    assert len(groups[ItemType.NOTIFICATION]) == 1
    assert len(groups[ItemType.PHOTO]) == 1


def test_normalize_maps_every_domain():
    snapshot = make_snapshot(source_items=default_source_items())
    request = normalize(snapshot)

    assert request.task_id == snapshot.task_id
    assert request.date == "2026-06-20"  # recordDate 앞 10자
    assert request.timezone == "Asia/Seoul"
    assert request.window.start == "20260620T000000"

    assert len(request.stays) == 1
    assert request.stays[0].address == "주소"
    assert request.stays[0].start_at == "2026-06-20T00:00:00"

    assert len(request.movements) == 1
    assert request.movements[0].transports == ["IN_VEHICLE"]
    assert request.movements[0].end.latitude == 37.6

    assert len(request.calendars) == 1
    assert request.calendars[0].location_text == "회의실"

    assert len(request.notifications) == 1
    assert request.notifications[0].app_name == "카카오톡"
    # 알림은 항목 startAt 을 postedAt 으로 물려받는다.
    assert request.notifications[0].posted_at == "2026-06-20T00:00:00"

    assert len(request.photos) == 1
    assert request.photos[0].client_photo_uri == "content://p"


def test_health_metric_branches():
    request = normalize(make_snapshot(source_items=default_source_items()))
    by_metric = {h.metric: h for h in request.healths}

    assert by_metric[HealthMetric.STEPS].value == 10145
    assert by_metric[HealthMetric.STEPS].duration_minutes is None
    assert by_metric[HealthMetric.SLEEP].duration_minutes == 210
    assert by_metric[HealthMetric.SLEEP].value is None


def test_bad_item_is_skipped_not_fatal():
    items = [
        source_item(1, ItemType.STAY, {"latitude": 200.0}),  # 위도 범위 위반 → skip
        source_item(2, ItemType.CALENDAR, {"title": "정상"}, end="2026-06-20T01:00:00"),
    ]
    request = normalize(make_snapshot(source_items=items))

    # 잘못된 위치 항목은 걸러지고, 정상 캘린더는 남는다.
    assert request.stays == []
    assert len(request.calendars) == 1
    assert request.calendars[0].title == "정상"


def test_no_window_yields_none():
    request = normalize(make_snapshot(source_items=[], timeline_window=None))
    assert request.window is None
    assert request.iter_source_items() == []
