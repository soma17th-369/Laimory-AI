"""eventType 별 지속시간 상한을 검사한다 (#61, #119).

프롬프트는 event 를 타입마다 정해진 길이 이내로 만들라고 지시한다. 하지만 그 지시를
지켰는지 재는 코드가 없으면, 하루가 8~12시간짜리 event 하나로 뭉개져도 결과를 볼 때까지
알 수 없다. 이 guard 는 그 초과를 드러내는 결정론적 안전망이다.

상한은 #118 이 Timeline v3 프롬프트와 `docs/ai-event-candidate.md` 에 정한 값이다.

**재기만 하고 자르거나 나누지 않는다.** 긴 event 를 어디서 끊을지는 의미 판단이라
코드가 정할 수 없다. 분할은 Repair 가 `OVEREXTENDED_EVENT` 로 잡아
`update_event`·`split_event` 로 처리한다.

면제는 네 가지다.

- 지속 구간이 근거에 직접 있는 타입(`_EXEMPT_EVENT_TYPES`). `MEAL` 은 `meal_guard` 가
  20~60분으로 이미 전담하므로 여기서 두 번 경고하지 않는다.
- **캘린더 근거가 있고 event 길이가 그 일정의 길이를 넘지 않는 event.** 타입을 가리지
  않는다. 일정이 09:00~23:00 이면 그 시간을 따르는 `WORK` 는 길어도 일정대로다.
  1시간짜리 일정을 근거로 댄 8시간 event 는 일정대로가 아니므로 면제하지 않는다.
- **MOVEMENT 근거가 있는 `EXERCISE`.** Location 이 왕복 도보 여정을 산책으로 표시한
  것이고, 산책은 왕복 구간을 그대로 쓴다.
- **코드가 하나로 합치는 체류.** 이동 없이 같은 장소에서 이어진 체류는 확정 pass 가
  하나의 event 로 합친다(`merge_stay_events`). 그 event 를 나눠도 다음 확정에서 다시
  합쳐지므로, 나누라고 알리면 Repair 가 고칠 수 없는 것을 고치려 든다. 실제 LLM 은
  3.4시간짜리 체류를 세 번 나눴고 세 번 다 도로 합쳐졌다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

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
from app.services.stay_merge import mergeable_stay_groups
from app.services.validator import parse_datetime, resolve_timezone

#: 타입별 절에 따로 적히지 않은 타입의 상한.
MAX_EVENT_DURATION = timedelta(hours=3)

#: 3시간보다 짧게 정한 타입.
_MAX_DURATION_BY_TYPE: dict[EventType, timedelta] = {
    EventType.PHOTO_MOMENT: timedelta(hours=1),  # 순간 기록이라 더 길면 활동 event 다
    EventType.MEETING: timedelta(hours=2),  # 더 길면 업무와 섞였을 가능성이 크다
    EventType.EXERCISE: timedelta(hours=2),  # 운동 한 번의 일반 길이
}

#: 지속시간이 근거에 직접 있어 상한을 적용하지 않는 event 종류.
_EXEMPT_EVENT_TYPES = frozenset(
    {
        EventType.CALENDAR_EVENT,  # 시작·종료가 일정에 명시돼 있다
        EventType.SLEEP,  # 수면은 직접 기록된 구간이다
        EventType.MOVEMENT,  # 실제 이동 구간은 통째로 품는다
        EventType.MEAL,  # meal_guard 가 20~60분으로 전담한다
    }
)

_WARNING_ID_PREFIX = "warning-event-duration-"


@dataclass(frozen=True)
class DurationFinding:
    """상한을 넘긴 event 하나."""

    event: TimelineEventDraft
    limit: timedelta
    duration: timedelta

    def detail(self) -> dict[str, str]:
        return {
            "eventType": self.event.event_type.value,
            "durationHours": _hours(self.duration),
            "limitHours": _hours(self.limit),
        }


def max_duration_for(event_type: EventType) -> timedelta | None:
    """타입의 상한. 상한을 재지 않는 타입이면 ``None``."""

    if event_type in _EXEMPT_EVENT_TYPES:
        return None
    return _MAX_DURATION_BY_TYPE.get(event_type, MAX_EVENT_DURATION)


def verify_event_duration(
    draft: TimelineDraft,
    request: TimelineDraftRequest | None = None,
    *,
    by_type: bool = True,
) -> list[DurationFinding]:
    """타입별 상한을 넘는 event 를 경고하고 이전 검사 결과를 재계산한다.

    `request` 가 없으면 일정의 길이를 알 수 없어 캘린더 면제를 적용하지 못한다.

    `by_type` 이 거짓이면 #119 이전처럼 잰다. 타입을 가리지 않고 3시간이고 면제는
    `_EXEMPT_EVENT_TYPES` 뿐이다. 타입별 상한은 v3 프롬프트가 정한 값이라, 그 지시를 받은
    적 없는 세트가 규칙대로 만든 event 에 warning 을 붙이지 않기 위해서다.
    """

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    calendar_spans = _calendar_spans(request) if by_type else {}
    stay_groups = _merged_stay_groups(request) if by_type else []
    findings: list[DurationFinding] = []

    for event in draft.events:
        limit = max_duration_for(event.event_type)
        if limit is None:
            continue
        if not by_type:
            limit = MAX_EVENT_DURATION

        duration = event.end_time - event.start_time
        if duration <= limit:
            continue
        if by_type and (
            _is_walk(event)
            or _follows_calendar(event, duration, calendar_spans)
            or _is_merged_stay(event, stay_groups)
        ):
            continue

        findings.append(DurationFinding(event=event, limit=limit, duration=duration))
        subject = f"{event.event_type.value} 상한" if by_type else "비캘린더 event 권장 상한"
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_ID_PREFIX}{len(findings):03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"'{event.title}' event 가 {_hours(duration)}시간으로 "
                    f"{subject} {_hours(limit)}시간을 "
                    "넘었습니다. 하나의 활동이 계속됐다는 근거가 없으면 나눠야 합니다."
                ),
                source_refs=list(event.source_refs),
            )
        )

    return findings


def _is_walk(event: TimelineEventDraft) -> bool:
    """이동을 근거로 댄 `EXERCISE` 인가. 산책은 왕복 구간을 그대로 쓴다."""

    return event.event_type is EventType.EXERCISE and any(
        ref.source_type is EventSourceType.MOVEMENT for ref in event.source_refs
    )


def _merged_stay_groups(request: TimelineDraftRequest | None) -> list[frozenset[str]]:
    """확정 pass 가 하나의 체류로 합치는 STAY 묶음."""

    if request is None:
        return []
    return mergeable_stay_groups(request, resolve_timezone(request.timezone))


def _is_merged_stay(
    event: TimelineEventDraft, stay_groups: list[frozenset[str]]
) -> bool:
    """근거가 전부 한 묶음의 STAY 인가. 그런 event 는 나눠도 코드가 다시 합친다."""

    if not event.source_refs or any(
        ref.source_type is not EventSourceType.STAY for ref in event.source_refs
    ):
        return False
    raw_ids = {ref.raw_id for ref in event.source_refs}
    return any(raw_ids <= group for group in stay_groups)


def _calendar_spans(
    request: TimelineDraftRequest | None,
) -> dict[str, tuple[datetime, datetime]]:
    """일정 rawId → (시작, 종료). 구간을 읽을 수 있는 일정만 담는다."""

    if request is None:
        return {}

    tz = resolve_timezone(request.timezone)
    spans: dict[str, tuple[datetime, datetime]] = {}
    for item in request.calendars:
        identifier = raw_id_of(item)
        if not identifier or not item.end_at:
            continue
        start = parse_datetime(item.start_at, tz)
        end = parse_datetime(item.end_at, tz)
        if start is None or end is None or end <= start:
            continue
        spans[identifier] = (start, end)
    return spans


def _follows_calendar(
    event: TimelineEventDraft,
    duration: timedelta,
    calendar_spans: dict[str, tuple[datetime, datetime]],
) -> bool:
    """event 가 참조한 일정의 길이 안에 있는가.

    일정이 여럿이면 가장 이른 시작부터 가장 늦은 종료까지를 하나의 길이로 본다. 같은 활동을
    가리키는 여러 일정을 하나의 event 로 합칠 수 있기 때문이다.
    """

    referenced = [
        calendar_spans[ref.raw_id]
        for ref in event.source_refs
        if ref.source_type is EventSourceType.CALENDAR and ref.raw_id in calendar_spans
    ]
    if not referenced:
        return False
    span = max(end for _, end in referenced) - min(start for start, _ in referenced)
    return duration <= span


def _hours(duration: timedelta) -> str:
    """소수 한 자리 시간 문자열. `3.0` 처럼 붙는 꼬리는 정리한다."""

    value = duration.total_seconds() / 3600
    return f"{value:.1f}".removesuffix(".0")
