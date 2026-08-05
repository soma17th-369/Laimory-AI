"""수집 원본을 도메인별 요청 모델로 정규화한다."""

from __future__ import annotations

import re

from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger
from app.core.execution_context import ExecutionStage
from app.schemas import (
    CalendarItem,
    CollectedSnapshot,
    CollectedSourceItem,
    HealthItem,
    ItemType,
    MovementItem,
    NotificationItem,
    PhotoItem,
    StayItem,
    TimelineDraftRequest,
    TimeWindow,
)
from app.services.source_contract import SourceBatchError

logger = get_logger(__name__)


def split_source_items(
    snapshot: CollectedSnapshot,
) -> dict[ItemType, list[CollectedSourceItem]]:
    """`sourceItems`를 `itemType` 기준으로 그룹화한다."""

    groups: dict[ItemType, list[CollectedSourceItem]] = {
        item_type: [] for item_type in ItemType
    }
    for item in snapshot.source_items:
        groups[item.item_type].append(item)
    return groups


def _base_payload(item: CollectedSourceItem) -> dict:
    return {
        "rawId": item.raw_id,
        "startAt": item.start_at,
        "endAt": item.end_at,
    }


def _stay(item: CollectedSourceItem) -> StayItem:
    return StayItem.model_validate(_base_payload(item) | item.payload)


def _movement(item: CollectedSourceItem) -> MovementItem:
    payload = dict(item.payload)
    transports = payload.get("transports")
    if isinstance(transports, str):
        payload["transports"] = [transports]
    return MovementItem.model_validate(_base_payload(item) | payload)


def _calendar(item: CollectedSourceItem) -> CalendarItem:
    return CalendarItem.model_validate(_base_payload(item) | item.payload)


def _normalize_health_payload(payload: dict) -> dict:
    normalized = dict(payload)
    metric = normalized.get("metric")
    value = normalized.get("value")
    if not isinstance(value, str):
        return normalized

    match = re.search(r"\d+(?:\.\d+)?", value)
    if match is None:
        return normalized

    number = float(match.group())
    if metric == "SLEEP" and "durationMinutes" not in normalized:
        normalized["durationMinutes"] = int(number)
        normalized.pop("value", None)
    else:
        normalized["value"] = number
    return normalized


def _health(item: CollectedSourceItem) -> HealthItem:
    return HealthItem.model_validate(
        _base_payload(item) | _normalize_health_payload(item.payload)
    )


def _notification(item: CollectedSourceItem) -> NotificationItem:
    return NotificationItem.model_validate(
        {
            "rawId": item.raw_id,
            "postedAt": item.start_at,
            **item.payload,
        }
    )


def _photo(item: CollectedSourceItem) -> PhotoItem:
    # payload 의 `photoUrl` 은 PhotoItem 이 그대로 받는다. `photoFile`/`fileName`/
    # `clientPhotoUri` 는 모델에 필드가 없어 조용히 무시된다(#52).
    return PhotoItem.model_validate(
        {"rawId": item.raw_id, "takenAt": item.start_at, **item.payload}
    )


_PARSERS = {
    ItemType.STAY: ("stays", _stay),
    ItemType.MOVEMENT: ("movements", _movement),
    ItemType.CALENDAR: ("calendars", _calendar),
    ItemType.HEALTH: ("healths", _health),
    ItemType.NOTIFICATION: ("notifications", _notification),
    ItemType.PHOTO: ("photos", _photo),
}


def _to_date(record_date: str) -> str:
    return record_date[:10]


def normalize(snapshot: CollectedSnapshot) -> TimelineDraftRequest:
    """수집 원본을 도메인별로 분리하고 검증해 TimelineDraftRequest를 만든다."""

    groups = split_source_items(snapshot)

    domains: dict[str, list] = {field: [] for field, _ in _PARSERS.values()}
    for item_type, items in groups.items():
        field, parser = _PARSERS[item_type]
        for item in items:
            try:
                domains[field].append(parser(item))
            except Exception as exc:  # noqa: BLE001 - 개별 항목 실패는 건너뛴다.
                report_error(
                    logger,
                    ErrorCode.SOURCE_ITEM_NORMALIZE_FAILED,
                    "수집 항목 정규화 실패",
                    exc=exc,
                    context={"itemType": item_type.value, "rawId": item.raw_id},
                    stage=ExecutionStage.REQUEST,
                )

    # window 는 접수 요청이 정본으로 주고 runner 가 스냅샷에 덮어쓴다. 여기까지 비어
    # 있다면 호출 경로가 잘못된 것이므로 검증 없는 타임라인을 만들지 않고 멈춘다(#67).
    if snapshot.timeline_window is None:
        raise SourceBatchError(
            "timelineWindow 가 없어 결과 범위를 강제할 수 없습니다",
            code=ErrorCode.SOURCE_CONTRACT_VIOLATION,
        )
    window = TimeWindow(
        start=snapshot.timeline_window.start_time,
        end=snapshot.timeline_window.end_time,
    )

    request = TimelineDraftRequest(
        task_id=snapshot.task_id,
        date=_to_date(snapshot.record_date),
        timezone=snapshot.record_time_zone,
        window=window,
        user_memory=snapshot.user_memory,
        **domains,
    )
    logger.debug(
        "정규화 완료: taskId=%s, stays=%d, movements=%d, calendars=%d, "
        "healths=%d, notifications=%d, photos=%d",
        request.task_id,
        len(request.stays),
        len(request.movements),
        len(request.calendars),
        len(request.healths),
        len(request.notifications),
        len(request.photos),
    )
    return request
