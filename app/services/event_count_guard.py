"""최종 event 개수 상한 검사 (#118).

프롬프트는 하루의 event 를 최대 24개로 구성하라고 지시한다. 지켰는지 재는 코드가 없으면
잘게 쪼개진 하루가 그대로 저장돼도 결과를 볼 때까지 모른다. 이 guard 는 그 초과를
드러내는 결정론적 안전망이다.

**재기만 하고 자르지 않는다.** 무엇을 버리고 무엇을 합칠지는 의미 판단이라, 코드가
confidence 순으로 잘라 내면 캘린더·사진처럼 반드시 남아야 할 근거를 잃는다
(`duration_guard` 와 같은 원칙). 합치는 것은 Repair 의 판단이다.
"""

from app.schemas import TimelineDraft, TimelineWarning, TimelineWarningSeverity

#: 하루 타임라인의 최대 event 수.
MAX_EVENT_COUNT = 24

_WARNING_ID_PREFIX = "warning-event-count-"


def verify_event_count(draft: TimelineDraft) -> None:
    """상한을 넘는 event 수를 경고하고 이전 검사 결과를 재계산한다."""

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    count = len(draft.events)
    if count <= MAX_EVENT_COUNT:
        return

    draft.warnings.append(
        TimelineWarning(
            warning_id=f"{_WARNING_ID_PREFIX}001",
            severity=TimelineWarningSeverity.MEDIUM,
            message=(
                f"event 가 {count}개로 하루 최대 {MAX_EVENT_COUNT}개를 넘었습니다. "
                "같은 장소의 이어진 체류, 같은 대화방의 대화, 같은 장면의 사진, "
                "한 여정의 짧은 이동부터 합쳐야 합니다."
            ),
        )
    )
