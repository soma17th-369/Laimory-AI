"""결과 저장 요청을 보내기 전의 자체검증.

App Server 결과 저장 API 를 호출하기 전에 각 이벤트의 필수값과 source 소속을
확인한다. 같은 source가 여러 이벤트의 근거가 되는 것은 허용한다.

입력에 없는 ``rawId``는 App Server 도 거절하지만, 여기서 먼저 막는다. 거절될 것이
뻔한 요청을 왕복시키면 실패 원인이 네트워크 계층으로 밀려 진단이 어려워진다.

window 이탈과 수면 event 도 여기서 다시 본다(#67). `repair_draft` 가 이미 강제하지만
그것은 **repair 를 거쳤을 때의 이야기**다. 저장 직전의 이 지점이 마지막 관문이라,
앞 단계가 어떻게 바뀌어도 범위 밖 event 와 수면이 저장되지 않게 한다.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.schemas.timeline import TimelineDraft
from app.schemas.timeline_request import TimelineDraftRequest
from app.services.sleep_exclusion import HIDDEN_EVENT_TYPES
from app.services.validator import WindowBounds, resolve_window_bounds


class TimelineViolationCode(StrEnum):
    """원문 값 없이 운영 관측에 남길 수 있는 저장 계약 위반 코드."""

    TITLE_EMPTY = "TITLE_EMPTY"
    START_TIME_MISSING = "START_TIME_MISSING"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    SOURCE_REFS_EMPTY = "SOURCE_REFS_EMPTY"
    SOURCE_RAW_ID_NOT_IN_TASK = "SOURCE_RAW_ID_NOT_IN_TASK"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    HIDDEN_EVENT_TYPE = "HIDDEN_EVENT_TYPE"


@dataclass(frozen=True)
class TimelineViolation:
    code: TimelineViolationCode
    message: str


class TimelineValidationError(AppError):
    """최종 타임라인 저장 계약 위반."""

    default_code = ErrorCode.TIMELINE_STORAGE_VALIDATION_FAILED

    def __init__(self, violations: list[TimelineViolation]) -> None:
        self.details = violations
        self.violations = [violation.message for violation in violations]
        self.violation_codes = sorted({violation.code.value for violation in violations})
        super().__init__("타임라인 저장 전 검증 실패: " + "; ".join(self.violations))


def _collect_timeline_violations(
    draft: TimelineDraft,
    valid_raw_ids: set[str],
    window: WindowBounds | None = None,
) -> list[TimelineViolation]:
    """저장 위반 코드와 진단 문자열을 함께 만든다."""

    violations: list[TimelineViolation] = []
    for event in draft.events:
        event_id = event.client_event_id

        if event.event_type in HIDDEN_EVENT_TYPES:
            violations.append(
                TimelineViolation(
                    TimelineViolationCode.HIDDEN_EVENT_TYPE,
                    f"이벤트 {event_id}: {event.event_type.value} 은 저장하지 않습니다",
                )
            )

        if (
            window is not None
            and event.start_time is not None
            and (event.start_time < window.start or event.end_time > window.end)
        ):
            # 시각 원문은 사용자 데이터라 남기지 않는다. 어느 event 인지만 남긴다.
            violations.append(
                TimelineViolation(
                    TimelineViolationCode.OUTSIDE_WINDOW,
                    f"이벤트 {event_id}: 요청 window 밖입니다",
                )
            )

        if not (event.title or "").strip():
            violations.append(
                TimelineViolation(
                    TimelineViolationCode.TITLE_EMPTY,
                    f"이벤트 {event_id}: title 이 비어 있습니다",
                )
            )
        if event.start_time is None:
            violations.append(
                TimelineViolation(
                    TimelineViolationCode.START_TIME_MISSING,
                    f"이벤트 {event_id}: startTime 이 없습니다",
                )
            )
        if (
            event.end_time is not None
            and event.start_time is not None
            and event.end_time < event.start_time
        ):
            violations.append(
                TimelineViolation(
                    TimelineViolationCode.INVALID_TIME_RANGE,
                    f"이벤트 {event_id}: endTime 이 startTime 보다 빠릅니다",
                )
            )

        if not event.source_refs:
            violations.append(
                TimelineViolation(
                    TimelineViolationCode.SOURCE_REFS_EMPTY,
                    f"이벤트 {event_id}: source 가 없습니다",
                )
            )
            continue

        for raw_id in {ref.raw_id for ref in event.source_refs}:
            if raw_id not in valid_raw_ids:
                violations.append(
                    TimelineViolation(
                        TimelineViolationCode.SOURCE_RAW_ID_NOT_IN_TASK,
                        f"이벤트 {event_id}: source rawId={raw_id} 가 현재 task 입력에 없습니다",
                    )
                )

    return violations


def validate_timeline_for_storage(
    draft: TimelineDraft,
    valid_raw_ids: set[str],
    request: TimelineDraftRequest | None = None,
) -> list[str]:
    """저장 위반 사유를 반환한다. 빈 리스트면 저장할 수 있다.

    `request` 를 주면 요청 window 이탈까지 검사한다. 운영 경로는 항상 준다.
    """

    return [
        violation.message
        for violation in _collect_timeline_violations(
            draft, valid_raw_ids, _window_of(request)
        )
    ]


def ensure_timeline_valid_for_storage(
    draft: TimelineDraft,
    valid_raw_ids: set[str],
    request: TimelineDraftRequest | None = None,
) -> None:
    """저장 계약을 검증하고 위반 시 ``TimelineValidationError``를 던진다."""

    violations = _collect_timeline_violations(
        draft, valid_raw_ids, _window_of(request)
    )
    if violations:
        raise TimelineValidationError(violations)


def _window_of(request: TimelineDraftRequest | None) -> WindowBounds | None:
    return resolve_window_bounds(request) if request is not None else None
