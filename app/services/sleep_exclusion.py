"""수면·기상 비노출 경계 (#67).

수면 정보는 믿을 수 없다. 실제 실행 로그에서 Sleep/Activity Agent 의 수면 후보는
0건인데 Calendar Agent 가 사용자가 적어 둔 `수면 zzz` 일정을 `SLEEP` 으로 분류했고,
Timeline 은 그것을 실제 수면 행동으로 단정했다. 사용자가 캘린더에 적은 글자와 실제로
잔 시각은 다르다.

그래서 두 가지를 함께 한다.

    1. `SLEEP`·`WAKE_UP` event 를 최종 결과에서 뺀다.
    2. 그 근거로 쓰인 rawId 를 **제외 집합**으로 만들어, 다른 event 가 그것을 근거로
       삼지 못하게 한다.

2번이 없으면 1번은 눈속임이다. 수면 근거가 다른 event 에 섞여 들어가 그 event 의
시간을 새벽으로 끌어내리기 때문이다.

제외 집합에 넣는 것은 **수면의 의미를 지닌 근거**뿐이다. 수면 후보가 참조한 STAY 나
NOTIFICATION 까지 빼면, 그 근거로 만들어진 멀쩡한 낮 event 가 함께 사라진다. 그래서
후보가 참조한 rawId 중 `SLEEP`·`CALENDAR` 타입만 걷어 낸다.

제외 집합은 Repair 의 매 확정 패스에서 현재 Event Agent 결과로 다시 계산한다.
`rerun_event_agent` 로 결과가 갈리거나 Timeline 을 다시 돌려도 정책이 유지된다.

`sleep_guard` 는 지우지 않는다. 정확한 수면 데이터가 복구되면 다시 쓸 수 있게 독립
서비스로 남긴다.
"""

from collections.abc import Iterable

from app.core.logging import get_logger
from app.schemas import (
    AgentEventResult,
    EventSourceType,
    EventType,
    HealthMetric,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: 사용자 결과에 내보내지 않는 event 종류.
HIDDEN_EVENT_TYPES = frozenset({EventType.SLEEP, EventType.WAKE_UP})

#: 수면 후보의 근거 중 "수면이라는 의미"를 지닌 source 타입.
#: STAY·MOVEMENT·NOTIFICATION 은 수면 후보가 인용했더라도 다른 event 의 정당한
#: 근거이므로 제외 집합에 넣지 않는다.
_SLEEP_EVIDENCE_SOURCE_TYPES = frozenset(
    {EventSourceType.SLEEP, EventSourceType.CALENDAR}
)

#: Calendar 수면 일정 판별 keyword. Event Agent 가 분류에 실패했을 때만 쓰는
#: 방어 경로다. 흔한 오탐을 피하려고 좁게 잡았다 — `잠` 하나만으로는 판단하지
#: 않는다(`잠실`·`잠깐`), `기상`도 쓰지 않는다(`기상청`).
_SLEEP_TITLE_KEYWORDS = ("수면", "취침", "낮잠", "잠자", "잠들", "sleep", "zzz")

#: 한 warning 에 담을 예시 개수.
_MAX_EXAMPLES = 3


def _examples(titles: list[str]) -> str:
    shown = ", ".join(titles[:_MAX_EXAMPLES])
    if len(titles) > _MAX_EXAMPLES:
        shown += f" 외 {len(titles) - _MAX_EXAMPLES}건"
    return shown


def is_sleep_calendar_title(title: str | None) -> bool:
    """캘린더 제목이 수면 일정으로 보이는가. Agent 분류 실패 시의 방어 경로다."""

    text = (title or "").casefold()
    return any(keyword in text for keyword in _SLEEP_TITLE_KEYWORDS)


def sleep_excluded_raw_ids(
    event_results: Iterable[AgentEventResult],
    request: TimelineDraftRequest,
) -> frozenset[str]:
    """다른 event 의 근거로 쓰면 안 되는 수면 rawId 집합.

    세 곳에서 모은다.

        1. Event Agent 가 `SLEEP`·`WAKE_UP` 으로 분류한 후보의 수면성 근거
        2. health 입력의 수면 기록 자체
        3. 제목이 수면으로 보이는 캘린더 일정(Agent 분류 실패 대비)
    """

    excluded: set[str] = set()

    for result in event_results:
        for candidate in result.candidates:
            if candidate.event_type not in HIDDEN_EVENT_TYPES:
                continue
            for ref in candidate.source_refs:
                if ref.source_type in _SLEEP_EVIDENCE_SOURCE_TYPES:
                    excluded.add(str(ref.raw_id))

    for item in request.healths:
        if item.metric is not HealthMetric.SLEEP:
            continue  # 걸음 수(ACTIVITY)는 하루 맥락 근거로 계속 쓴다
        identifier = raw_id_of(item)
        if identifier:
            excluded.add(identifier)

    for item in request.calendars:
        if not is_sleep_calendar_title(item.title):
            continue
        identifier = raw_id_of(item)
        if identifier:
            excluded.add(identifier)

    if excluded:
        # rawId 는 입력 식별자라 값 자체는 남기지 않는다. 몇 건인지만 남긴다.
        logger.debug("수면 제외 rawId %d건을 계산했습니다.", len(excluded))
    return frozenset(excluded)


def apply_sleep_exclusion(
    draft: TimelineDraft, excluded_raw_ids: frozenset[str]
) -> None:
    """수면·기상 event 를 빼고 수면 근거를 걷어 낸다(in-place).

    근거를 걷어 낸 결과 유효한 근거가 하나도 남지 않은 event 는 제거한다. 근거 없는
    event 는 저장 계약(`timeline_validator`)도 거절하므로 여기서 정리하는 편이 낫다.
    """

    kept = []
    hidden: list[str] = []
    emptied: list[str] = []

    for event in draft.events:
        if event.event_type in HIDDEN_EVENT_TYPES:
            hidden.append(event.title)
            continue

        if excluded_raw_ids:
            remaining = [
                ref for ref in event.source_refs if ref.raw_id not in excluded_raw_ids
            ]
            if len(remaining) != len(event.source_refs):
                if not remaining:
                    emptied.append(event.title)
                    continue
                event.source_refs = remaining

        kept.append(event)

    if not hidden and not emptied:
        return

    draft.events = kept

    seq = len(draft.warnings)
    if hidden:
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-sleep-hidden-{seq:03d}",
                severity=TimelineWarningSeverity.LOW,
                message=(
                    f"수면·기상 event {len(hidden)}건을 결과에서 제외했습니다: "
                    f"{_examples(hidden)}"
                ),
            )
        )
    if emptied:
        seq += 1
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-sleep-hidden-{seq:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    f"수면 근거를 제외하니 남은 근거가 없어 event {len(emptied)}건을 "
                    f"제거했습니다: {_examples(emptied)}"
                ),
            )
        )

    logger.debug(
        "수면 비노출 적용: 제외=%d, 근거소진 제거=%d",
        len(hidden),
        len(emptied),
    )
