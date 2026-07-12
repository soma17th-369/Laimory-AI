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


def source_identifier(item) -> str | None:
    """항목의 정식 식별자. rawId 가 원칙이고, 없을 때만 내부 `id` 로 대체한다.

    수집 원본이 rawId 를 주지 않는 항목은 참조할 방법이 아예 없어지므로, 그때만
    `id` 를 식별자로 인정한다. rawId 가 있는 항목의 `id` 는 식별자가 아니다
    (LLM 이 프롬프트에 함께 실려 나간 내부 순번 `id` 를 `rawId` 로 착각하는 일이
    실제로 있으나, 그런 참조는 어느 항목과도 매칭되지 않아 조용히 무시된다).
    """

    raw_id = getattr(item, "raw_id", None)
    if raw_id:
        return raw_id
    item_id = getattr(item, "id", None)
    return str(item_id) if item_id is not None else None


def build_owner_index(request: TimelineDraftRequest) -> dict[str, set[EventSourceType]]:
    """식별자 → 그 식별자를 가진 입력의 sourceType 들.

    rawId 는 보통 UUID 라 소유 타입이 하나뿐이다. rawId 가 없어 내부 `id` 로 대체한
    항목끼리는 같은 식별자를 가질 수 있어 집합으로 둔다.
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
            if identifier := source_identifier(item):
                owners[identifier].add(source_type)

    for item in request.healths:
        identifier = source_identifier(item)
        source_type = _HEALTH_SOURCE_TYPES.get(item.metric)
        if identifier and source_type is not None:
            owners[identifier].add(source_type)

    return dict(owners)


def normalize_source_types(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """draft 의 sourceRef sourceType 을 입력의 실제 타입으로 맞춘다(in-place).

    LLM 이 이동을 `STAY` 라고 적어 놓으면 이후 repair 는 그 이동을 영영 찾지 못한다.
    산책 event 가 왕복 이동을 근거로 대 놓고도 편도에서 끊기는 이유가 그것이다.

    rawId 를 찾을 수 없으면(환각) 그대로 둔다. 근거 조회에서 조용히 무시될 뿐이다.
    소유 타입이 둘 이상이라 모호하면 역시 손대지 않는다.
    """

    owners = build_owner_index(request)
    repaired = 0

    for event in draft.events:
        for ref in event.source_refs:
            candidates = owners.get(ref.source_id)
            if not candidates or len(candidates) != 1:
                continue
            (actual,) = candidates
            if actual is not ref.source_type:
                ref.source_type = actual
                repaired += 1

    if repaired:
        logger.info("sourceRef 의 sourceType %d건을 입력의 실제 타입으로 맞췄습니다.", repaired)
