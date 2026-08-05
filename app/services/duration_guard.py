"""비캘린더 event 의 지속시간 상한을 검사한다 (#61).

프롬프트는 지속 구간이 분명하지 않은 event 를 3시간 이내로 만들라고 지시한다. 하지만
그 지시를 지켰는지 재는 코드가 없으면, 하루가 8~12시간짜리 event 하나로 뭉개져도
결과를 볼 때까지 알 수 없다. 이 guard 는 그 초과를 드러내는 결정론적 안전망이다.

**재기만 하고 자르거나 나누지 않는다.** 긴 event 를 어디서 끊을지는 의미 판단이라
코드가 정할 수 없다. 분할은 Repair 가 `OVEREXTENDED_EVENT` 로 잡아
`update_event`·`rerun_timeline_agent` 로 처리한다.

지속 구간이 근거에 직접 있는 event 는 제외한다. 판단 기준은 **enum 이 아니라 실제
근거**다(#67). `eventType` 이 `CALENDAR_EVENT` 라고 적혀 있다는 것과 그 event 가 정말
캘린더 일정을 근거로 든다는 것은 다르다. LLM 이 라벨만 붙인 event 까지 상한에서
빼 주면, 근거 없는 장시간 event 가 조용히 통과한다.

`MEAL` 은 `meal_guard` 가 20~60분으로 이미 전담하므로 여기서 두 번 경고하지 않는다.
"""

from datetime import timedelta

from app.schemas import (
    EventSourceType,
    EventType,
    TimelineDraft,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)

MAX_EVENT_DURATION = timedelta(hours=3)

#: 근거 종류와 무관하게 상한을 적용하지 않는 event 종류.
_EXEMPT_EVENT_TYPES = frozenset(
    {
        EventType.MOVEMENT,  # 실제 이동 구간은 통째로 품는다
        EventType.MEAL,  # meal_guard 가 20~60분으로 전담한다
    }
)

#: 지속 구간을 직접 제공하는 근거 종류. 이 근거를 실제로 인용한 event 만 면제한다.
_SPAN_EVIDENCE_TYPES = frozenset(
    {
        EventSourceType.CALENDAR,  # 시작·종료가 일정에 명시돼 있다
        EventSourceType.SLEEP,  # 수면은 직접 기록된 구간이다
    }
)


def _has_span_evidence(event: TimelineEventDraft) -> bool:
    """지속 구간을 직접 제공하는 근거를 실제로 인용하는가."""

    return any(ref.source_type in _SPAN_EVIDENCE_TYPES for ref in event.source_refs)

_WARNING_ID_PREFIX = "warning-event-duration-"


def verify_event_duration(draft: TimelineDraft) -> None:
    """3시간을 넘는 비캘린더 event 를 경고하고 이전 검사 결과를 재계산한다."""

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    sequence = 0
    for event in draft.events:
        if event.event_type in _EXEMPT_EVENT_TYPES or _has_span_evidence(event):
            continue

        duration = event.end_time - event.start_time
        if duration <= MAX_EVENT_DURATION:
            continue

        sequence += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_ID_PREFIX}{sequence:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"'{event.title}' event 가 {_hours(duration)}시간으로 "
                    f"권장 상한 {_hours(MAX_EVENT_DURATION)}시간을 넘었습니다. "
                    "지속 구간을 직접 제공하는 근거가 없으므로 하나의 활동이 계속됐다는 "
                    "근거가 없으면 나눠야 합니다."
                ),
                source_refs=list(event.source_refs),
            )
        )


def _hours(duration: timedelta) -> str:
    """소수 한 자리 시간 문자열. `3.0` 처럼 붙는 꼬리는 정리한다."""

    value = duration.total_seconds() / 3600
    return f"{value:.1f}".removesuffix(".0")
