"""식사(MEAL) event 지속시간 가드.

식사는 몇 시간씩 이어지지 않는다. 그런데 하루의 뼈대인 위치 데이터에서는 식당·카페
체류가 길게 잡히고, 그 체류 안에 음식 사진 한 장이 섞이면 LLM 은 **체류 전체를 하나의
`MEAL` event** 로 만들기 쉽다. "3시간 동안 점심 식사" 같은 draft 가 그렇게 나온다.

프롬프트(`timeline.md`)가 "식사는 짧고, 긴 체류는 체류대로 두고 그 안의 짧은 식사
event 를 따로 만든다" 를 안내하고, 이 모듈이 draft repair 단계에서 그 길이를
결정론적으로 강제한다. 문장을 새로 지어내야 하는 event 분리는 프롬프트가 맡고,
코드는 **길이 보장**만 책임진다.

    - 60분을 넘는 `MEAL` 은 **시점 근거**(음식 사진 촬영 시각, 결제 알림 시각)를
      기준으로 최대 60분 창으로 줄인다.
    - 시점 근거가 없으면 event 시작부터 60분으로 줄이고 `confidence` 를 낮춘다.
      식사 시각을 특정할 근거가 없다는 뜻이기 때문이다.
    - 20분보다 짧은 `MEAL`(예: 사진 한 장이 만든 순간 event)은 20분으로 늘린다.

조정한 event 에는 `uncertainty` 를 남기고 draft `warnings` 로 알린다. 이 가드는
요청 window 검증 **앞**에서 돌아, 늘어난 시간의 최종 경계는 window 검증이 정한다.
"""

from datetime import datetime, timedelta, tzinfo

from app.core.logging import get_logger
from app.schemas import (
    EventSourceType,
    EventType,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.source_lookup import raw_id_of
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)

#: 식사 event 로 인정하는 지속시간 범위.
MEAL_MIN_DURATION = timedelta(minutes=20)
MEAL_MAX_DURATION = timedelta(minutes=60)

#: 시점 근거 없이 길이만 줄인 식사에 허용하는 confidence 상한.
_UNANCHORED_MAX_CONFIDENCE = 0.6

_ANCHORED_NOTE = (
    "식사 시간은 음식 사진·알림 시각을 기준으로 최대 60분까지만 잡았다. "
    "실제 체류는 더 길었을 수 있다."
)
_UNANCHORED_NOTE = (
    "식사 시각을 특정할 근거가 없어 체류 시작 시각 기준 60분으로 제한했다."
)
_EXPANDED_NOTE = "식사 시각 근거가 한 시점뿐이라 최소 20분으로 잡았다."


def _minutes(duration: timedelta) -> int:
    return round(duration.total_seconds() / 60)


def _anchor_times(request: TimelineDraftRequest) -> dict[EventSourceType, dict[str, str]]:
    """식사 시각을 특정할 수 있는 **시점 근거**의 rawId → 시각 문자열 표.

    사진은 촬영 시각이, 알림은 도착 시각이 식사가 일어난 순간을 가리킨다. 체류/이동은
    구간이라 식사 시각을 좁혀 주지 못하므로 근거로 쓰지 않는다.
    """

    return {
        EventSourceType.PHOTO: {
            identifier: photo.taken_at
            for photo in request.photos
            if (identifier := raw_id_of(photo))
        },
        EventSourceType.NOTIFICATION: {
            identifier: notification.posted_at
            for notification in request.notifications
            if (identifier := raw_id_of(notification))
        },
    }


def _event_anchors(
    event: TimelineEventDraft,
    anchors: dict[EventSourceType, dict[str, str]],
    tz: tzinfo,
) -> list[datetime]:
    """event 가 근거로 삼은 시점들 중 event 시간 안에 있는 것만 시간순으로 모은다."""

    moments: list[datetime] = []
    for ref in event.source_refs:
        raw = anchors.get(ref.source_type, {}).get(ref.raw_id)
        if raw is None:
            continue
        moment = parse_datetime(raw, tz)
        if moment is None:
            continue
        if event.start_time <= moment <= event.end_time:
            moments.append(moment)
    return sorted(moments)


def _fit(start: datetime, end: datetime, lower: datetime, upper: datetime) -> tuple[datetime, datetime]:
    """[start, end] 를 [lower, upper] 안으로 넣되 최소 지속시간을 지킨다."""

    start = max(lower, start)
    end = min(upper, end)
    if end - start < MEAL_MIN_DURATION:
        end = min(upper, start + MEAL_MIN_DURATION)
    if end - start < MEAL_MIN_DURATION:
        start = max(lower, end - MEAL_MIN_DURATION)
    return start, end


def _shrink(event: TimelineEventDraft, moments: list[datetime]) -> None:
    """너무 긴 식사 event 를 근거 시각 기준 최대 60분 창으로 줄인다."""

    lower, upper = event.start_time, event.end_time

    if moments:
        start = moments[0]
        end = min(moments[-1], start + MEAL_MAX_DURATION)
        note = _ANCHORED_NOTE
    else:
        start = lower
        end = start + MEAL_MAX_DURATION
        event.confidence = min(event.confidence, _UNANCHORED_MAX_CONFIDENCE)
        note = _UNANCHORED_NOTE

    event.start_time, event.end_time = _fit(start, end, lower, upper)
    event.uncertainty.append(note)


def _expand(event: TimelineEventDraft) -> None:
    """순간에 가까운 식사 event 를 최소 20분으로 늘린다."""

    event.end_time = event.start_time + MEAL_MIN_DURATION
    event.uncertainty.append(_EXPANDED_NOTE)


def enforce_meal_duration(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """draft 의 `MEAL` event 지속시간을 20~60분으로 강제한다(in-place)."""

    tz = resolve_timezone(request.timezone)
    anchors = _anchor_times(request)
    adjusted: list[tuple[TimelineEventDraft, timedelta]] = []

    for event in draft.events:
        if event.event_type is not EventType.MEAL:
            continue

        before = event.end_time - event.start_time
        if MEAL_MIN_DURATION <= before <= MEAL_MAX_DURATION:
            continue

        if before > MEAL_MAX_DURATION:
            _shrink(event, _event_anchors(event, anchors, tz))
        else:
            _expand(event)
        adjusted.append((event, before))

    seq = len(draft.warnings)
    for event, before in adjusted:
        after = event.end_time - event.start_time
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-meal-{seq:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"식사 event 시간을 {_minutes(before)}분에서 {_minutes(after)}분으로 "
                    f"조정했습니다: {event.title}"
                ),
                source_refs=list(event.source_refs),
            )
        )
        logger.info(
            "식사 event 지속시간 조정: title=%s, %d분 -> %d분",
            event.title,
            _minutes(before),
            _minutes(after),
        )
