"""수면 경계 가드.

자고 있는 사람은 아무것도 하지 않는다. 수면 구간 안에는 `SLEEP` 말고 어떤 event 도
있을 수 없고, 하루의 나머지 event 는 전부 기상 이후에 일어난다.

그런데 알림은 사람이 자는 동안에도 도착한다. 실제 live 출력에서 새벽 2시 32분에 온
카카오톡 푸시 하나가 `"아침부터 이어진 연락과 준비"` event 의 시작 시각을 새벽 2시로
끌어내렸다. 나머지 근거 두 건은 07:35, 07:52 로 멀쩡히 기상 후였다. 알림 하나가
event 를 다섯 시간 앞으로 당긴 것이다.

각 Event Agent 는 자기 source 만 본다. 알림 Agent 는 수면 기록을 아예 받지 못하므로
"이 알림이 도착했을 때 사용자는 자고 있었다"를 알 방법이 없다. 프롬프트로 고칠 수 있는
문제가 아니다. 수면 구간을 아는 것은 입력 전체를 쥔 repair 단계뿐이라, 여기서
결정론적으로 강제한다.

    - 기상 시각 이전은 통째로 금지 구간이다. 수면 **시작 전**(잠들기 직전의 체류 등)도
      포함한다. 사용자가 "수면을 제외한 모든 event 는 기상 후에 일어난다"로 확정했다.
    - 금지 구간에 통째로 들어간 event 는 제거하고, 걸쳐 있는 event 는 깨어 있는 쪽만
      남긴다(예: 02:32~07:52 → 06:50~07:52).
    - `SLEEP` 은 수면 그 자체이므로 면제한다. `WAKE_UP` 은 수면이 끝난 시점이므로
      기상 시각으로 스냅한다.

기상 시각은 **가장 이른** 수면 종료 시각으로 잡는다. 낮잠 기록이 섞여 들어와도 아침이
통째로 지워지지 않게 하기 위함이다. 낮잠 구간 자체는 별도의 금지 구간으로 남는다.
"""

from datetime import datetime, timedelta, tzinfo

from app.core.logging import get_logger
from app.schemas import (
    EventType,
    HealthMetric,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.validator import parse_datetime, renumber_events, resolve_timezone

logger = get_logger(__name__)

#: 기상 이전 금지 구간의 시작. 요청은 하루치라 이틀 전이면 어떤 event 보다 앞선다.
#: `datetime.min` 을 쓰지 않는 이유는 timezone 이 붙은 극단값 비교를 피하기 위함이다.
_PRE_WAKE_LOOKBACK = timedelta(days=2)

_CLAMPED_NOTE = "수면 중에 도착한 근거가 있어 시작 시각을 기상 시각으로 옮겼다."

#: 한 warning 에 담을 예시 개수.
_MAX_EXAMPLES = 3

Span = tuple[datetime, datetime]


def _examples(titles: list[str]) -> str:
    shown = ", ".join(titles[:_MAX_EXAMPLES])
    if len(titles) > _MAX_EXAMPLES:
        shown += f" 외 {len(titles) - _MAX_EXAMPLES}건"
    return shown


def sleep_spans(request: TimelineDraftRequest, tz: tzinfo) -> list[Span]:
    """입력의 수면 구간 목록. 시작/종료를 모두 아는 기록만 쓴다."""

    spans: list[Span] = []
    for item in request.healths:
        if item.metric is not HealthMetric.SLEEP or not item.end_at:
            continue
        start = parse_datetime(item.start_at, tz)
        end = parse_datetime(item.end_at, tz)
        if start is None or end is None or end <= start:
            continue
        spans.append((start, end))
    return spans


def wake_time(request: TimelineDraftRequest, tz: tzinfo) -> datetime | None:
    """기상 시각. 수면 기록이 없으면 ``None``."""

    spans = sleep_spans(request, tz)
    if not spans:
        return None
    return min(end for _, end in spans)


def _forbidden_spans(spans: list[Span], wake: datetime) -> list[Span]:
    """event 가 있을 수 없는 구간들. 기상 이전 전체 + 각 수면 구간."""

    return [(wake - _PRE_WAKE_LOOKBACK, wake), *spans]


def _is_asleep(moment: datetime, forbidden: list[Span]) -> bool:
    """한 시점이 금지 구간 안인가. 경계(기상 시각)는 깨어 있는 쪽으로 본다."""

    return any(start <= moment < end for start, end in forbidden)


def _carve(start: datetime, end: datetime, forbidden: list[Span]) -> Span | None:
    """구간에서 금지 구간을 도려낸다. 깨어 있는 시간이 남지 않으면 ``None``."""

    for forbidden_start, forbidden_end in forbidden:
        if end <= forbidden_start or start >= forbidden_end:
            continue  # 겹치지 않는다
        if forbidden_start <= start and end <= forbidden_end:
            return None  # 자는 동안에만 있었다
        if start < forbidden_start and forbidden_end < end:
            start = forbidden_end  # 수면을 가로지른다. 깨어난 뒤를 남긴다.
        elif start < forbidden_start:
            end = forbidden_start  # 잠들기 전까지만 깨어 있었다
        else:
            start = forbidden_end  # 깨어난 뒤부터 이어졌다

    return (start, end) if start < end else None


def enforce_sleep_boundary(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """수면 중 event 를 제거하고 기상 시각에 걸친 event 를 잘라 낸다(in-place).

    수면 기록이 없으면 아무것도 하지 않는다. 기상 시각을 모르면 강제할 근거도 없다.
    """

    tz = resolve_timezone(request.timezone)
    spans = sleep_spans(request, tz)
    if not spans:
        return

    wake = min(end for _, end in spans)
    forbidden = _forbidden_spans(spans, wake)

    kept = []
    removed: list[str] = []
    clamped: list[str] = []

    for event in draft.events:
        if event.event_type is EventType.SLEEP:
            kept.append(event)
            continue

        if event.event_type is EventType.WAKE_UP:
            event.start_time = event.end_time = wake
            kept.append(event)
            continue

        if event.start_time == event.end_time:  # 사진 순간 같은 0 길이 event
            if _is_asleep(event.start_time, forbidden):
                removed.append(event.title)
                continue
            kept.append(event)
            continue

        awake = _carve(event.start_time, event.end_time, forbidden)
        if awake is None:
            removed.append(event.title)
            continue

        if awake != (event.start_time, event.end_time):
            event.start_time, event.end_time = awake
            event.uncertainty.append(_CLAMPED_NOTE)
            clamped.append(event.title)
        kept.append(event)

    if not removed and not clamped:
        return

    draft.events = kept
    renumber_events(draft)

    seq = len(draft.warnings)
    if removed:
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-sleep-{seq:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"수면 중이라 일어날 수 없는 event {len(removed)}건을 제외했습니다: "
                    f"{_examples(removed)}"
                ),
            )
        )
    if clamped:
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-sleep-{seq:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"수면 구간에 걸친 event {len(clamped)}건의 시간을 기상 이후로 "
                    f"잘랐습니다: {_examples(clamped)}"
                ),
            )
        )

    logger.info(
        "수면 경계 강제: 기상=%s, 제거=%d, 클램프=%d",
        wake.isoformat(),
        len(removed),
        len(clamped),
    )
