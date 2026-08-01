"""AI 결과 시간 범위 검증.

요청 timeline window(시작/끝)를 코드 레벨에서 강제한다. 프롬프트만으로는
window 밖 event 가 새어 나올 수 있어, 두 지점에서 검증한다.

    1. Event Agent 후보 단계(`filter_result_to_window`): window 밖 후보/단서를
       생성 단계에서 제거한다(= 범위 밖 event 를 만들지 않음).
    2. 최종 draft 단계(`validate_draft_to_window`): LLM 이 만든 draft 의 각
       event `startTime`/`endTime` 을 다시 검증해, 완전히 벗어난 event 는 제거하고
       경계에 걸친 event 는 warning 으로 남긴다.

window 문자열은 `"20260630T000000"`(basic) 또는 `"2026-06-30T00:00:00"`(extended)
등 형태가 유동적이라 관대하게 파싱하고, 파싱에 실패하면 검증을 건너뛴다
(데이터 한 건이 전체 흐름을 막지 않도록).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger
from app.core.execution_context import ExecutionStage
from app.schemas import (
    AgentEventResult,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)

logger = get_logger(__name__)

_DEFAULT_TZ = "Asia/Seoul"
_PARSE_FORMATS = ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d", "%Y-%m-%d")


@dataclass(frozen=True)
class WindowBounds:
    """요청 window 의 시작/끝 (tz-aware)."""

    start: datetime
    end: datetime


def resolve_timezone(name: str | None) -> tzinfo:
    """요청 timezone 을 해석한다. 입력 시각 문자열을 파싱하는 쪽이 함께 쓴다.

    timezone database가 없는 환경에서는 Asia/Seoul을 UTC+09:00으로 처리한다.
    """

    key = name or _DEFAULT_TZ
    try:
        return ZoneInfo(key)
    except Exception as exc:  # noqa: BLE001 - tzdata 미설치/알 수 없는 timezone 방어
        if key != _DEFAULT_TZ:
            # 기본값(Asia/Seoul)이 tzdata 미설치로 실패하는 건 이 환경의 상수라
            # 매번 남길 필요가 없다. 요청이 준 다른 timezone 이 깨졌을 때만 남긴다.
            report_error(
                logger,
                ErrorCode.TIMEZONE_RESOLUTION_FAILED,
                "timezone 로드 실패로 KST(+09:00)를 사용합니다",
                exc=exc,
                context={"timezone": key, "fallback": _DEFAULT_TZ},
                stage=ExecutionStage.REQUEST,
            )
        return timezone(timedelta(hours=9), _DEFAULT_TZ)


def parse_datetime(value: str, tz: tzinfo) -> datetime | None:
    """수집 원본의 시각 문자열을 tz-aware datetime 으로 관대하게 파싱한다.

    window 문자열과 도메인 항목의 `startAt`/`takenAt` 등이 형태가 제각각이라
    (`"20260630T000000"`, `"2026-06-30T00:00:00"`, `"2026-07-08T17:27:20.309"`)
    실패해도 예외 대신 ``None`` 을 돌려준다. 파싱 실패 한 건이 전체 검증을
    막지 않게 하기 위해서다.
    """

    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        for fmt in _PARSE_FORMATS:
            try:
                dt = datetime.strptime(value, fmt)
                break
            except (TypeError, ValueError):
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def resolve_window_bounds(request: TimelineDraftRequest) -> WindowBounds | None:
    """요청에서 window 경계를 구한다. window 가 없거나 파싱 실패면 None."""

    if request.window is None:
        return None
    tz = resolve_timezone(request.timezone)
    start = parse_datetime(request.window.start, tz)
    end = parse_datetime(request.window.end, tz)
    if start is None or end is None:
        # window 원문은 요청이 준 값이라 남기지 않는다. 파싱에 실패했다는 사실만으로
        # 어디를 봐야 하는지는 정해진다(요청 body 는 App Server 쪽에 있다).
        logger.warning("window 파싱 실패로 범위 검증을 건너뜁니다.")
        return None
    if end < start:
        logger.warning("window.end 가 start 보다 앞서 범위 검증을 건너뜁니다.")
        return None
    return WindowBounds(start=start, end=end)


def _classify(start: datetime, end: datetime, bounds: WindowBounds) -> str:
    """구간이 window 에 대해 inside/partial/outside 중 무엇인지 판정한다."""

    if end < bounds.start or start > bounds.end:
        return "outside"
    if start < bounds.start or end > bounds.end:
        return "partial"
    return "inside"


def _clamp_to_window(start: datetime, end: datetime, bounds: WindowBounds) -> tuple[datetime, datetime]:
    """window와 겹치는 구간을 window 안쪽으로 자른다."""

    return max(start, bounds.start), min(end, bounds.end)


def filter_result_to_window(
    result: AgentEventResult, bounds: WindowBounds
) -> tuple[AgentEventResult, int]:
    """window 를 완전히 벗어난 후보/단서를 제거한 새 결과와 제거 건수를 반환한다."""

    kept_candidates = []
    for candidate in result.candidates:
        cls = _classify(
            candidate.time_range.start_time,
            candidate.time_range.end_time,
            bounds,
        )
        if cls == "outside":
            continue
        if cls == "partial":
            start, end = _clamp_to_window(
                candidate.time_range.start_time,
                candidate.time_range.end_time,
                bounds,
            )
            candidate.time_range.start_time = start
            candidate.time_range.end_time = end
        kept_candidates.append(candidate)

    kept_fragments = []
    for fragment in result.fragments:
        if fragment.time_range is None:
            kept_fragments.append(fragment)
            continue
        cls = _classify(
            fragment.time_range.start_time,
            fragment.time_range.end_time,
            bounds,
        )
        if cls == "outside":
            continue
        if cls == "partial":
            start, end = _clamp_to_window(
                fragment.time_range.start_time,
                fragment.time_range.end_time,
                bounds,
            )
            fragment.time_range.start_time = start
            fragment.time_range.end_time = end
        kept_fragments.append(fragment)
    dropped = (len(result.candidates) - len(kept_candidates)) + (
        len(result.fragments) - len(kept_fragments)
    )
    filtered = AgentEventResult(
        candidates=kept_candidates,
        fragments=kept_fragments,
        warnings=list(result.warnings),
    )
    return filtered, dropped


def renumber_events(draft: TimelineDraft) -> None:
    """남은 event 에 `clientEventId` 를 1번부터 다시 매긴다(in-place).

    event 를 제거한 뒤에는 번호에 구멍이 생기므로 항상 이 함수로 다시 매긴다.
    질문의 `relatedEventIds` 도 새 id 로 보정하고, 사라진 event 참조는 버린다.
    draft 에서 event 를 제거하는 검증(window·sourceRef)이 공유한다.
    """

    id_map: dict[str, str] = {}
    for index, event in enumerate(draft.events, start=1):
        new_id = f"event-{index:03d}"
        id_map[event.client_event_id] = new_id
        event.client_event_id = new_id

    for question in draft.questions:
        question.related_event_ids = [
            id_map[old] for old in question.related_event_ids if old in id_map
        ]


def validate_draft_to_window(draft: TimelineDraft, bounds: WindowBounds) -> None:
    """draft 의 event 시간을 검증한다(in-place).

    - 완전히 window 밖인 event 는 제거하고, `clientEventId` 를 다시 매겨
      `questions.relatedEventIds` 를 함께 보정한다.
    - 경계에 걸친 event 는 유지하되 warning 을 남긴다.

    warning 은 `clientEventId` 대신 **제목**으로 event 를 가리킨다. 이 검증 뒤에
    repair 가 event 를 다시 정렬하고 id 를 다시 매기므로, 여기서 적어 둔 id 는
    사용자가 볼 때쯤이면 다른 event 를 가리킨다.
    """

    kept = []
    dropped = 0
    partial: list[TimelineEventDraft] = []

    for event in draft.events:
        cls = _classify(event.start_time, event.end_time, bounds)
        if cls == "outside":
            dropped += 1
            continue
        if cls == "partial":
            event.start_time, event.end_time = _clamp_to_window(
                event.start_time,
                event.end_time,
                bounds,
            )
            partial.append(event)
        kept.append(event)

    draft.events = kept
    renumber_events(draft)

    seq = len(draft.warnings)
    if dropped:
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-window-{seq:03d}",
                severity=TimelineWarningSeverity.HIGH,
                message=f"요청 시간 범위(window)를 벗어난 event {dropped}건을 제외했습니다.",
            )
        )
    if partial:
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-window-{seq:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    "요청 시간 범위 경계에 걸친 event 가 있습니다: "
                    + ", ".join(event.title for event in partial)
                ),
            )
        )
