"""사용자에게 보이는 타임라인 문장 길이를 검사한다.

문체와 문장 수는 의미 판단이 필요한 영역이라 Timeline·Repair Agent가 맡는다.
이 guard는 저장 상한에 닿기 전에 명백한 장문을 드러내는 결정론적 안전망으로,
description의 앞뒤 공백을 제외한 길이가 120자를 넘을 때만 LOW warning을 남긴다.

프롬프트가 모델에게 주는 목표는 100자 내외다. 여기서 120자를 기준으로 삼는 것은
목표를 조금 넘긴 문장까지 경고로 만들지 않기 위한 여유다.
"""

from app.schemas import TimelineDraft, TimelineWarning, TimelineWarningSeverity

DESCRIPTION_WARNING_LENGTH = 120
_WARNING_ID_PREFIX = "warning-narrative-length-"


def verify_narrative_length(draft: TimelineDraft) -> None:
    """120자를 넘는 description을 경고하고 이전 검사 결과를 재계산한다."""

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    sequence = 0
    for event in draft.events:
        length = len((event.description or "").strip())
        if length <= DESCRIPTION_WARNING_LENGTH:
            continue

        sequence += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"{_WARNING_ID_PREFIX}{sequence:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"'{event.title}' event description이 {length}자로 "
                    f"권장 경고 기준 {DESCRIPTION_WARNING_LENGTH}자를 넘었습니다."
                ),
                source_refs=list(event.source_refs),
            )
        )
