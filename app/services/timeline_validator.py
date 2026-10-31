"""결과 저장 요청을 보내기 전의 자체검증.

App Server 결과 저장 API 를 호출하기 전에 각 이벤트의 필수값과 source 소속을
확인한다. 같은 source가 여러 이벤트의 근거가 되는 것은 허용한다.

입력에 없는 ``rawId``는 App Server 도 거절하지만, 여기서 먼저 막는다. 거절될 것이
뻔한 요청을 왕복시키면 실패 원인이 네트워크 계층으로 밀려 진단이 어려워진다.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.schemas.common import PlacePoint
from app.schemas.timeline import TimelineDraft
from app.schemas.timeline_result import MAX_PLACE_CANDIDATES, TEXT_MAX, PlaceCandidate
from app.services.place_text import normalize_place_text


class TimelineViolationCode(StrEnum):
    """원문 값 없이 운영 관측에 남길 수 있는 저장 계약 위반 코드."""

    TITLE_EMPTY = "TITLE_EMPTY"
    START_TIME_MISSING = "START_TIME_MISSING"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    SOURCE_REFS_EMPTY = "SOURCE_REFS_EMPTY"
    SOURCE_RAW_ID_NOT_IN_TASK = "SOURCE_RAW_ID_NOT_IN_TASK"


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
    draft: TimelineDraft, valid_raw_ids: set[str]
) -> list[TimelineViolation]:
    """저장 위반 코드와 진단 문자열을 함께 만든다."""

    violations: list[TimelineViolation] = []
    for event in draft.events:
        event_id = event.client_event_id

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


class PlaceCandidateViolation(StrEnum):
    """후보를 뺀 이유. 원문 값 없이 운영 관측에 남긴다."""

    BLANK_NAME = "BLANK_NAME"
    DUPLICATE_NAME = "DUPLICATE_NAME"
    NAME_NOT_IN_EVIDENCE = "NAME_NOT_IN_EVIDENCE"
    TOO_MANY = "TOO_MANY"


def checked_place_candidates(
    names: list[str], sources: Mapping[str, PlacePoint | None]
) -> tuple[list[PlaceCandidate], Counter[PlaceCandidateViolation]]:
    """후보 이름을 그 event 의 입력과 대조해 계약 모양으로 옮긴다(사용자 결정 2026-10-31).

    `sources` 는 그 event 가 쓸 수 있는 이름(정규화한 값)과 입력 원소의 좌표다. 입력에 없는 이름, 빈
    이름, 중복, 5개를 넘는 후보는 뺀다. 좌표는 언제나 입력의 그 원소 값이고 코드가 만들거나 고치지
    않는다. 뺀 것을 실패로 올리지 않는다 — 후보 하나 때문에 하루 기록을 버리지 않는다.
    """

    kept: list[PlaceCandidate] = []
    seen: set[str] = set()
    dropped: Counter[PlaceCandidateViolation] = Counter()
    for name in names:
        text = name.strip()
        key = normalize_place_text(text)
        if not text or not key:
            dropped[PlaceCandidateViolation.BLANK_NAME] += 1
        elif key in seen:
            dropped[PlaceCandidateViolation.DUPLICATE_NAME] += 1
        elif key not in sources:
            dropped[PlaceCandidateViolation.NAME_NOT_IN_EVIDENCE] += 1
        elif len(kept) == MAX_PLACE_CANDIDATES:
            dropped[PlaceCandidateViolation.TOO_MANY] += 1
        else:
            seen.add(key)
            point = sources[key]
            kept.append(
                PlaceCandidate(
                    name=text[:TEXT_MAX],
                    latitude=point.latitude if point else None,
                    longitude=point.longitude if point else None,
                )
            )
    return kept, dropped


def validate_timeline_for_storage(
    draft: TimelineDraft, valid_raw_ids: set[str]
) -> list[str]:
    """저장 위반 사유를 반환한다. 빈 리스트면 저장할 수 있다."""

    return [
        violation.message
        for violation in _collect_timeline_violations(draft, valid_raw_ids)
    ]


def ensure_timeline_valid_for_storage(
    draft: TimelineDraft, valid_raw_ids: set[str]
) -> None:
    """저장 계약을 검증하고 위반 시 ``TimelineValidationError``를 던진다."""

    violations = _collect_timeline_violations(draft, valid_raw_ids)
    if violations:
        raise TimelineValidationError(violations)
