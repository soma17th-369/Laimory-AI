"""사용자에게 보이는 타임라인 문장 길이를 확정한다.

문체와 문장 수는 의미 판단이 필요한 영역이라 Timeline·Repair Agent가 맡는다. 길이는
다르다. `title` 30자·`description` 120자는 제품이 약속한 하드 상한이고, 확률적으로
지켜지길 기다릴 값이 아니다(#67).

두 단계로 나눈다.

    1. `verify_narrative_length` — Repair 반복 중에는 **경고만** 남긴다. LLM 이 문장을
       자연스럽게 다시 쓸 기회를 먼저 준다. 잘라 버리면 그 기회가 사라진다.
    2. `enforce_narrative_length` — 반복이 끝난 뒤에도 넘치면 결정론적으로 줄인다.
       문장 경계 → 단어 경계 → 하드 절단 순으로 시도해, 가능한 한 말이 되는 곳에서
       끊는다.

event 를 통째로 버리지 않는다. 제품이 원하는 것은 event 삭제가 아니라 문장 축약이다.
스키마 검증으로 거절하면 길게 쓴 event 하나가 사용자 하루에서 사라진다.
"""

from app.schemas import TimelineDraft, TimelineWarning, TimelineWarningSeverity

#: 사용자 노출 문자열의 하드 상한. 이 두 값이 정본이다 — 저장 변환도 같은 값을 쓴다.
TITLE_MAX_LENGTH = 30
DESCRIPTION_MAX_LENGTH = 120

#: 예전 이름. 경고 기준과 하드 상한이 같은 값이라 그대로 둔다.
DESCRIPTION_WARNING_LENGTH = DESCRIPTION_MAX_LENGTH

_WARNING_ID_PREFIX = "warning-narrative-length-"

#: 문장이 끝났다고 볼 만한 글자. 해요체 과거형이라 마침표가 대부분이다.
_SENTENCE_ENDINGS = (". ", "! ", "? ", ".", "!", "?")


def verify_narrative_length(draft: TimelineDraft) -> None:
    """상한을 넘는 문장을 경고한다. 자르지는 않는다.

    Repair 반복마다 자기 이전 warning 을 지우고 현재 draft 로 다시 잰다. 병합·문장
    수정으로 길이가 바뀐 뒤 stale warning 이 남지 않게 하기 위함이다.
    """

    draft.warnings = [
        warning
        for warning in draft.warnings
        if not warning.warning_id.startswith(_WARNING_ID_PREFIX)
    ]

    sequence = 0
    for event in draft.events:
        for field, limit, value in (
            ("title", TITLE_MAX_LENGTH, event.title),
            ("description", DESCRIPTION_MAX_LENGTH, event.description),
        ):
            length = len((value or "").strip())
            if length <= limit:
                continue

            sequence += 1
            draft.warnings.append(
                TimelineWarning(
                    warning_id=f"{_WARNING_ID_PREFIX}{sequence:03d}",
                    severity=TimelineWarningSeverity.LOW,
                    message=(
                        f"'{event.title}' event {field}이 {length}자로 "
                        f"상한 {limit}자를 넘었습니다."
                    ),
                    source_refs=list(event.source_refs),
                )
            )


def enforce_narrative_length(draft: TimelineDraft) -> None:
    """Repair 가 끝난 뒤에도 남은 초과 문장을 결정론적으로 줄인다(in-place)."""

    truncated: list[str] = []
    for event in draft.events:
        title = (event.title or "").strip()
        if len(title) > TITLE_MAX_LENGTH:
            event.title = shorten(title, TITLE_MAX_LENGTH)
            truncated.append(event.title)

        description = (event.description or "").strip()
        if len(description) > DESCRIPTION_MAX_LENGTH:
            event.description = shorten(description, DESCRIPTION_MAX_LENGTH)
            if event.title not in truncated:
                truncated.append(event.title)

    if not truncated:
        return

    shown = ", ".join(truncated[:3])
    if len(truncated) > 3:
        shown += f" 외 {len(truncated) - 3}건"
    draft.warnings.append(
        TimelineWarning(
            warning_id=f"{_WARNING_ID_PREFIX}truncated",
            severity=TimelineWarningSeverity.LOW,
            message=(
                f"Repair 가 줄이지 못한 문장 {len(truncated)}건을 상한에 맞춰 "
                f"줄였습니다: {shown}"
            ),
        )
    )


def shorten(text: str, limit: int) -> str:
    """`limit` 안으로 줄인다. 문장 → 단어 → 하드 절단 순으로 끊을 곳을 찾는다."""

    text = text.strip()
    if len(text) <= limit:
        return text

    head = text[:limit]

    # 1) 상한 안에서 문장이 끝나는 지점이 있으면 거기서 끊는다.
    #    찾지 못한 종결부(rfind == -1)는 후보에서 뺀다 — 넣으면 `-1 + len(ending)` 이
    #    양수가 되어 작은 limit 에서 엉뚱한 위치를 고른다.
    ends = [
        head.rfind(ending) + len(ending)
        for ending in _SENTENCE_ENDINGS
        if head.rfind(ending) != -1
    ]
    if ends and max(ends) > limit // 2:
        return head[: max(ends)].strip()

    # 2) 단어 경계. 한국어는 어절 단위라 공백이 그럭저럭 맞는 자리다.
    space = head.rfind(" ")
    if space > limit // 2:
        return head[:space].strip()

    # 3) 끊을 곳이 없으면 하드 절단이 불가피하다.
    return head.strip()
