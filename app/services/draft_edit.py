"""draft event 수정·삭제 (결정론).

Repair Agent 가 "이 event 의 이 필드를 이렇게 고쳐라", "이 event 는 지워라" 라고
말하면 실제 적용은 여기서 한다. LLM 이 draft 전체를 다시 써 내려가게 두지 않는
이유는 그러면 **손대지 않기로 한 event 까지 조용히 바뀌기** 때문이다. 수정은 지정한
event 의 지정한 필드에만 닿고, 나머지 값은 원본 그대로 남는다.

바꿀 수 있는 필드는 `_EDITABLE_FIELDS` 로 한정한다. `clientEventId` 는 편집 대상이
아니다. 그 id 는 repair 파이프라인이 정렬 결과에 맞춰 다시 부여하는 값이라
(`validator.renumber_events`), LLM 이 임의로 바꾸면 질문의 `relatedEventIds` 가
가리키는 곳이 어긋난다.

여기서는 **id 를 다시 매기지 않는다.** 한 번의 개선 계획이 여러 도구 호출을 담기
때문이다. 삭제할 때마다 번호를 다시 매기면 같은 계획 안의 다음 호출이 가리키는
`clientEventId` 가 다른 event 를 뜻하게 된다. 번호 재부여는 도구 실행이 모두 끝난 뒤
`repair_draft` 가 한 번에 한다.
"""

from typing import Any

from pydantic import ValidationError

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.schemas import TimelineDraft, TimelineEventDraft

logger = get_logger(__name__)

#: Repair Agent 가 바꿀 수 있는 event 필드(입력 JSON 의 camelCase 이름).
_EDITABLE_FIELDS = frozenset(
    {
        "eventType",
        "title",
        "description",
        "address",
        "place",
        "tags",
        "startTime",
        "endTime",
        "confidence",
        "inferenceLevel",
        "sourceRefs",
        "uncertainty",
    }
)


class DraftEditError(AppError):
    """수정·삭제를 적용할 수 없을 때. 도구 계층이 잡아 실패 결과로 돌려준다."""

    default_code = ErrorCode.DRAFT_EDIT_FAILED


def find_event(draft: TimelineDraft, client_event_id: str) -> TimelineEventDraft:
    """`clientEventId` 로 event 를 찾는다. 없으면 `DraftEditError`."""

    for event in draft.events:
        if event.client_event_id == client_event_id:
            return event
    raise DraftEditError(f"clientEventId '{client_event_id}' 인 event 가 draft 에 없습니다.")


def update_event(
    draft: TimelineDraft,
    client_event_id: str,
    fields: dict[str, Any],
) -> TimelineEventDraft:
    """event 한 건의 지정한 필드만 바꾼다(in-place). 바뀐 event 를 돌려준다.

    스키마 검증(시간 순서·enum·confidence 범위 등)은 `TimelineEventDraft` 가 그대로
    한다. 검증에 실패하면 원본 event 를 **건드리지 않고** `DraftEditError` 를 던진다.
    잘못된 수정으로 event 를 반쯤 망가뜨리는 것보다, 고치지 못했다고 알리는 편이 낫다.
    """

    if not fields:
        raise DraftEditError("바꿀 필드가 없습니다.")

    unknown = sorted(set(fields) - _EDITABLE_FIELDS)
    if unknown:
        raise DraftEditError(
            f"바꿀 수 없는 필드입니다: {', '.join(unknown)}. "
            f"가능한 필드: {', '.join(sorted(_EDITABLE_FIELDS))}"
        )

    event = find_event(draft, client_event_id)
    payload = event.model_dump(by_alias=True, mode="json")
    payload.update(fields)
    payload["clientEventId"] = client_event_id  # id 는 편집 대상이 아니다

    try:
        updated = TimelineEventDraft.model_validate(payload)
    except ValidationError as exc:
        raise DraftEditError(f"수정한 event 가 스키마 검증에 실패했습니다: {exc}") from exc

    index = draft.events.index(event)
    draft.events[index] = updated
    logger.debug(
        "Repair: event 수정 clientEventId=%s, fields=%s",
        client_event_id,
        ", ".join(sorted(fields)),
    )
    return updated


def delete_event(draft: TimelineDraft, client_event_id: str) -> TimelineEventDraft:
    """event 한 건을 지운다(in-place). 지워진 event 를 돌려준다.

    지워진 event 를 가리키던 질문의 `relatedEventIds` 에서 그 id 를 뺀다. 참조가
    모두 사라진 질문 자체는 남긴다. 질문은 "이 시간대에 무엇을 했는가" 를 묻는
    것이라, event 가 사라졌다고 해서 물어볼 것이 없어지는 것은 아니다.
    """

    event = find_event(draft, client_event_id)
    draft.events.remove(event)

    for question in draft.questions:
        if client_event_id in question.related_event_ids:
            question.related_event_ids = [
                event_id
                for event_id in question.related_event_ids
                if event_id != client_event_id
            ]

    # 제목은 사용자 콘텐츠라 남기지 않는다. 무엇이 지워졌는지는 Langfuse 의 도구
    # 실행 기록에서 본다.
    logger.debug("Repair: event 삭제 clientEventId=%s", client_event_id)
    return event
