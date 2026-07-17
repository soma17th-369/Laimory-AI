from tests.agents.live_input_helpers import (
    calendar_request,
    full_live_request,
    health_request,
    notification_request,
    photo_request,
    stay_request,
)


def test_live_input_json_is_normalized_with_raw_ids() -> None:
    request = full_live_request()

    assert request.task_id == "2026-07-08"
    assert request.date == "2026-07-08"
    assert request.timezone == "Asia/Seoul"
    assert request.window.start == "2026-07-08T00:00"
    assert request.window.end == "2026-07-08T23:53:28.969"

    assert len(request.stays) == 6
    assert len(request.movements) == 4
    assert len(request.calendars) == 1
    assert len(request.healths) == 2
    assert len(request.notifications) == 18
    assert len(request.photos) == 4
    notification_raw_ids = {item.raw_id for item in request.notifications}
    photo_raw_ids = {item.raw_id for item in request.photos}
    health_by_metric = {item.metric.value: item for item in request.healths}

    assert "37af39db-f018-4973-bdf1-95724c74f824" in notification_raw_ids
    assert "e015a889-3517-45ac-9e12-ea94702fb7e7" in photo_raw_ids
    assert health_by_metric["SLEEP"].duration_minutes == 340
    assert health_by_metric["STEPS"].value == 10631
    assert request.stays[0].place == "오산운암3단지 주공아파트"
    assert request.movements[0].start.place == "오산운암3단지 주공아파트"


def test_event_agent_live_requests_keep_only_their_own_domains() -> None:
    stay = stay_request()
    assert len(stay.stays) == 6
    assert len(stay.movements) == 4
    assert stay.calendars == []
    assert stay.healths == []
    assert stay.notifications == []
    assert stay.photos == []

    assert len(calendar_request().calendars) == 1
    assert len(photo_request().photos) == 4
    assert len(notification_request().notifications) == 18
    assert len(health_request().healths) == 2
