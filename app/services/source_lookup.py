"""sourceRef 를 입력 항목으로 되짚기 위한 식별자 규칙.

repair 단계의 여러 모듈(`draft_repair`, `meal_guard`, `place_resolver`,
`calendar_location`)이 `sourceRef.rawId` 로 원본 입력 항목을 찾는다. 그 역참조가
서로 다른 규칙을 쓰면 같은 event 를 두고 모듈마다 다른 근거를 보게 되므로,
식별자 규칙 하나를 여기 모아 둔다.

`sourceType` 은 LLM 이 붙인 라벨이라 믿을 수 없다. 실제 출력에서 왕복 산책 event 는
근거 rawId 세 개를 정확히 인용해 놓고 이동 두 건을 `STAY` 라고 적었다. rawId 는
UUID 라 타입을 가로질러 유일하므로, **rawId 로 입력을 찾아 그 입력의 타입을 믿으면**
된다. 이것은 검증이 아니라 조회다. 아무것도 버리지 않고 아무 경고도 남기지 않는다.
"""

from collections import defaultdict

from app.core.logging import get_logger
from app.schemas import EventSourceType, HealthMetric, TimelineDraft, TimelineDraftRequest

logger = get_logger(__name__)

#: HEALTH 는 하나의 itemType 이지만 metric 으로 SLEEP/ACTIVITY 를 나눈다.
_HEALTH_SOURCE_TYPES = {
    HealthMetric.SLEEP: EventSourceType.SLEEP,
    HealthMetric.STEPS: EventSourceType.ACTIVITY,
}


def raw_id_of(item) -> str | None:
    """항목의 유일한 source 식별자인 rawId를 반환한다."""

    return getattr(item, "raw_id", None)


def build_owner_index(request: TimelineDraftRequest) -> dict[str, set[EventSourceType]]:
    """식별자 → 그 식별자를 가진 입력의 sourceType 들.

    rawId 는 항상 UUID이고 source item의 유일한 식별자다. 내부 DB `id`는 이 인덱스나
    Agent 계약에 들어오지 않는다. 잘못된 중복 입력도 안전하게 다루도록 sourceType은
    집합으로 유지한다.
    """

    owners: dict[str, set[EventSourceType]] = defaultdict(set)

    grouped: list[tuple[EventSourceType, list]] = [
        (EventSourceType.STAY, request.stays),
        (EventSourceType.MOVEMENT, request.movements),
        (EventSourceType.CALENDAR, request.calendars),
        (EventSourceType.NOTIFICATION, request.notifications),
        (EventSourceType.PHOTO, request.photos),
    ]
    for source_type, items in grouped:
        for item in items:
            if identifier := raw_id_of(item):
                owners[identifier].add(source_type)

    for item in request.healths:
        identifier = raw_id_of(item)
        source_type = _HEALTH_SOURCE_TYPES.get(item.metric)
        if identifier and source_type is not None:
            owners[identifier].add(source_type)

    return dict(owners)


def normalize_source_types(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """draft 의 sourceRef sourceType 을 입력의 실제 타입으로 맞춘다(in-place).

    LLM 이 이동을 `STAY` 라고 적어 놓으면 이후 repair 는 그 이동을 영영 찾지 못한다.
    산책 event 가 왕복 이동을 근거로 대 놓고도 편도에서 끊기는 이유가 그것이다.

    이 함수보다 먼저 source integrity 단계가 입력에 없는 rawId 참조를 제거한다.
    소유 타입이 둘 이상이라 모호한 잘못된 입력은 타입을 임의로 고치지 않는다.
    """

    owners = build_owner_index(request)
    repaired = 0

    for event in draft.events:
        for ref in event.source_refs:
            candidates = owners.get(ref.raw_id)
            if not candidates or len(candidates) != 1:
                continue
            (actual,) = candidates
            if actual is not ref.source_type:
                ref.source_type = actual
                repaired += 1

    if repaired:
        logger.info("sourceRef 의 sourceType %d건을 입력의 실제 타입으로 맞췄습니다.", repaired)
