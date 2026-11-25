"""Timeline Agent.

This agent merges ``AiEventCandidate`` values produced by data-specific Event
Agents into a user-editable ``TimelineDraft``. The LLM decides the semantic
merge; this module owns only the prompt, the tolerant parse, and the fallback
draft used when the LLM response is unusable.

정렬·겹침·지속시간·sourceRef 같은 **결정론적 확정**은 여기서 하지 않는다. main agent
그래프의 마지막 node 가 `app/services/draft_repair.py` 로 처리한다. 이 agent 가
돌려주는 draft 는 아직 다듬어지지 않은 LLM 결과이며, `clientEventId` 도 파싱 순서로
붙인 임시 값이다.
"""

import json
from uuid import UUID

from pydantic import ValidationError

from app.agents.base import Agent
from app.agents.parsing import (
    SupportsComplete,
    default_llm,
    items_to_text,
    user_memory_to_text,
)
from app.agents.prompt_loader import load_prompt
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import code_of_or, report_error
from app.core.logging import get_logger
from app.core.structured import StructuredOutputError
from app.core.llm_stages import LLMStage
from app.schemas import (
    AgentEventResult,
    ConversationDecision,
    EventType,
    PRE_CLUE_EVENT_TYPES,
    TimelineAgentOutput,
    TimelineConversationReview,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineEventNarration,
    TimelineNarrationOutput,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.schemas.clues import NotificationClueKind
from app.services.base_events import NEEDS_WRITING, BaseEvent, base_event_draft
from app.services.clue_bundles import (
    TimelineClues,
    expand_evidence,
    notification_evidence,
    timeline_input_sections,
)
from app.services.draft_edit import add_conversation_event

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "timeline.md")

_DEFAULT_USER_ID = "user-1234"
_DEFAULT_TIMEZONE = "Asia/Seoul"

#: 글이 빈 대화 event 의 제목 길이. 단서의 말을 옮겨 둘 뿐이고 Repair 가 다시 쓴다.
_TITLE_MAX = 30


def build_timeline_prompt(
    request: TimelineDraftRequest,
    user_memory_text: str,
    candidates_text: str,
    fragments_text: str,
) -> str:
    """Build the user prompt for timeline merging."""

    window_start = request.window.start if request.window else "정보 없음"
    window_end = request.window.end if request.window else "정보 없음"
    return (
        "[draft metadata]\n"
        f"userId: {_DEFAULT_USER_ID}\n"
        f"date: {request.date}\n"
        f"timezone: {_DEFAULT_TIMEZONE}\n"
        f"windowStart: {window_start}\n"
        f"windowEnd: {window_end}\n\n"
        f"[user memory]\n{user_memory_text}\n\n"
        f"[AI Event candidates]\n{candidates_text}\n\n"
        f"[Source fragments]\n{fragments_text}\n\n"
        "Create a user-reviewable TimelineDraft JSON. "
        "Merge candidates and fragments from different sources that point to the same "
        "real-life event into ONE timeline event when they share overlapping time, the "
        "same place, the same activity meaning, or repeatedly indicate the same situation "
        "(e.g. movement + photo + notification + activity in the same window). "
        "Preserve every merged sourceRef, span the merged event from earliest startTime to "
        "latest endTime, and prefer a human event type over raw types. Do not force-merge "
        "unrelated activities; when sources conflict, pick one and record the conflict in "
        "warnings or uncertainty instead. "
        "Sort events by actual candidate time, and expose uncertain or missing context "
        "through warnings and uncertainty. "
        "Do not copy example dates; use the metadata date and the provided candidate times."
    )


def build_timeline_clue_prompt(
    request: TimelineDraftRequest,
    user_memory_text: str,
    clues: TimelineClues,
) -> str:
    """v4 세트의 user prompt(#169, #202). 입력은 코드가 정한 하루의 event 목록이다."""

    sections = timeline_input_sections(clues)
    window_start = request.window.start if request.window else "정보 없음"
    window_end = request.window.end if request.window else "정보 없음"

    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)

    return (
        "[draft metadata]\n"
        f"date: {request.date}\n"
        f"timezone: {_DEFAULT_TIMEZONE}\n"
        f"windowStart: {window_start}\n"
        f"windowEnd: {window_end}\n\n"
        f"[하루]\n{dump(sections['day'])}\n\n"
        f"[event]\n{dump(sections['events'])}\n\n"
        f"[대화]\n{dump(sections['conversations'])}\n\n"
        f"[빈 시간]\n{dump(sections['emptyTimes'])}\n\n"
        f"[알림]\n{dump(sections['notifications'])}\n\n"
        f"[user memory]\n{user_memory_text}\n\n"
        "[event] 의 event 마다 종류와 글을 JSON 으로 쓰세요. event 는 id 로 가리키고, 대화는 "
        "conversations 에 먼저 정하세요."
    )


def parse_timeline_narration(
    text: str, request: TimelineDraftRequest, clues: TimelineClues
) -> TimelineDraft:
    """v4 LLM 응답을 draft 로 만든다(#202). 구조는 코드의 것이고 글만 응답에서 가져온다.

    event 는 언제나 코드가 정한 기본 event 전부이고 그 순서다. 응답은 id 로 기본 event 에 글을 얹을 뿐이다.

    - 모르는 id 는 warning 을 남기고 버린다. 같은 id 가 다시 오면 처음 것만 쓴다(사진마다 쪼갠 답).
    - 시간과 위치 기록·사진·일정 id 는 읽지 않는다. 근거로 더하는 것은 알림·대화뿐이다.
    - 글이 근거로 적은 사진(`photos`)에 그 event 의 사진이 아닌 id 가 있으면 그 글을 받지 않는다 — 옆 event 의
      사진을 읽어 쓴 글이다. 적은 id 는 `photo_ids` 로 남겨 확정 pass 가 빠진 사진을 짚는다.
    - 글이 오지 않았거나 틀 문장을 그대로 옮긴 기본 event 는 틀 글이다. 일정·사진이 있으면 `NEEDS_WRITING` 으로
      Repair 에 맡긴다.
    - 깨진 항목은 그것만 건너뛴다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
    payload = json.loads(text[start : end + 1])

    bundles = [bundle for bundle in clues.bundles if bundle.base_event is not None]
    known = {bundle.base_event.base_id for bundle in bundles}
    skipped: list[TimelineWarning] = []
    narrations: dict[str, TimelineEventNarration] = {}
    for index, raw in enumerate(payload.get("events") or [], start=1):
        if not isinstance(raw, dict):
            skipped.append(_invalid_item_warning("event", index))
            continue
        try:
            narration = TimelineEventNarration.model_validate(raw)
        except ValidationError as exc:
            _report_skipped_item("event", index, exc)
            skipped.append(_invalid_item_warning("event", index))
            continue
        key = narration.event_id.strip()
        if key not in known:
            skipped.append(
                TimelineWarning(
                    warning_id=f"warning-unknown-event-{index:03d}",
                    severity=TimelineWarningSeverity.LOW,
                    message=f"LLM 응답의 event 항목({index}번)이 입력에 없는 event id 를 가리켜 제외했습니다.",
                )
            )
            continue
        narrations.setdefault(key, narration)

    events: list[TimelineEventDraft] = []
    for number, bundle in enumerate(bundles, start=1):
        base = bundle.base_event
        event = base_event_draft(base, places=bundle.places, client_event_id=f"event-{number:03d}")
        if (narration := narrations.get(base.base_id)) is not None:
            foreign, _ = base.photo_mismatch([photo.photo_id for photo in narration.photos])
            if foreign:
                # 옆 event 의 사진을 읽어 쓴 글이다. 받지 않고 틀로 둔다 — 사진·일정이 있는 event 는 Repair 가 쓴다.
                skipped.append(_foreign_photo_warning(base.base_id, foreign))
            else:
                _write(event, base, narration, clues, request)
        events.append(event)

    warnings: list[TimelineWarning] = []
    for index, raw in enumerate(payload.get("warnings") or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = {**raw}
        item.setdefault("warningId", f"warning-{len(warnings) + 1:03d}")
        item.setdefault("severity", TimelineWarningSeverity.MEDIUM.value)
        # v4 는 근거를 event·단서 id 로 가리킨다. warning 에 그 id 를 적으면 rawId 로 펼친다 —
        # 그대로 두면 rawId 검증에 걸려 warning 이 통째로 버려진다(재실행에서 본 것).
        if item.get("sourceRefs"):
            item["sourceRefs"] = _warning_refs(item["sourceRefs"], clues, request)
        try:
            warnings.append(TimelineWarning.model_validate(item))
        except ValidationError as exc:
            _report_skipped_item("warning", index, exc)

    draft = TimelineDraft(
        user_id=_DEFAULT_USER_ID,
        date=request.date,
        timezone=request.timezone or _DEFAULT_TIMEZONE,
        events=events,
        warnings=[*warnings, *skipped],
        conversation_reviews=_conversation_reviews(payload),
    )
    _add_recorded_conversations(draft, clues)
    return draft


def _write(
    event: TimelineEventDraft,
    base: BaseEvent,
    narration: TimelineEventNarration,
    clues: TimelineClues,
    request: TimelineDraftRequest,
) -> None:
    """기본 event 에 Timeline 의 글을 얹는다. 시간과 위치 기록·사진·일정은 그대로다."""

    # 이동·산책의 종류는 구간이 정한다. 이동이 아닌 event 를 이동으로 쓰면 그 종류만 틀의 것으로 둔다.
    if base.fixed_type is not None:
        event.event_type = base.fixed_type
    elif narration.event_type is not EventType.MOVEMENT:
        event.event_type = narration.event_type
    event.title = narration.title.strip() or event.title
    event.description = narration.description.strip()
    event.confidence = narration.confidence
    event.inference_level = narration.inference_level
    event.uncertainty = list(narration.uncertainty)
    event.place_candidates = [name.strip() for name in narration.place_candidates if name.strip()]
    event.merge_reason = narration.reason.strip() or None
    event.photo_ids = [photo.photo_id for photo in narration.photos]
    # 틀 문장을 그대로 옮긴 글은 쓰지 않은 것이다. 일정·사진은 틀 문장으로 담기지 않는다.
    if not event.description or (base.needs_writing and event.description == base.description):
        event.merge_reason = NEEDS_WRITING
    refs, _ = notification_evidence(clues, narration.evidence, request)
    held = {ref.raw_id for ref in event.source_refs}
    event.source_refs.extend(ref for ref in refs if ref.raw_id not in held)


def _foreign_photo_warning(base_id: str, foreign: list[str]) -> TimelineWarning:
    return TimelineWarning(
        warning_id=f"warning-foreign-photo-{base_id}",
        severity=TimelineWarningSeverity.LOW,
        message=(
            f"LLM 응답의 {base_id} 글이 그 event 의 사진이 아닌 {', '.join(foreign)} 를 근거로 적어 받지 않았습니다."
        ),
    )

def _add_recorded_conversations(draft: TimelineDraft, clues: TimelineClues) -> None:
    """`RECORD` 로 정한 대화를 event 로 만든다(#183). 판단과 글은 Timeline 의 것이고 구조만 코드가 잡는다.

    시간과 근거는 그 대화에서 정한다. 이미 어느 event 의 근거인 대화는 만들지 않는다. 글이 비었어도 판단은
    살린다 — 단서의 말을 옮겨 두고 Repair 가 쓴다(`EVENT_TO_WRITE`). 개수는 확정 pass 가 맞춘다.
    """

    by_id = {
        clue.clue_id: clue
        for clue in clues.notifications
        if clue.kind is NotificationClueKind.CONVERSATION and clue.first_at is not None
    }
    for review in draft.conversation_reviews:
        clue = by_id.get(review.clue_id)
        if review.decision is not ConversationDecision.RECORD or clue is None:
            continue
        held = {ref.raw_id for event in draft.events for ref in event.source_refs}
        if held & set(clue.raw_ids):
            continue
        title = review.title.strip()
        description = review.description.strip()
        event = add_conversation_event(
            draft,
            clue,
            (title or clue.title or clue.counterpart or clue.clue_id)[:_TITLE_MAX],
            description or clue.summary or review.content.strip() or "",
        )
        event.client_event_id = f"event-{len(draft.events):03d}"
        event.merge_reason = (review.reason.strip() or None) if title and description else NEEDS_WRITING


def _conversation_reviews(payload: dict) -> list[TimelineConversationReview]:
    """대화 판단. 깨진 항목은 그것만 건너뛴다 — 없어도 event 는 그대로 쓸 수 있다."""

    reviews: list[TimelineConversationReview] = []
    for index, raw in enumerate(payload.get("conversations") or [], start=1):
        try:
            reviews.append(TimelineConversationReview.model_validate(raw))
        except ValidationError as exc:
            _report_skipped_item("conversation", index, exc)
    return reviews


def _warning_refs(
    source_refs: list, clues: TimelineClues, request: TimelineDraftRequest
) -> list[dict]:
    """warning 의 근거 id 를 rawId 로 펼친다. 모델이 적은 이유는 남긴다.

    일부러 쓰지 않은 근거의 이유는 Repair 가 다시 판단할 때 읽는 값이다(#183). 예전에는 단서의 종류
    이름(`대화 알림`)으로 덮였다. 단서 id 와 rawId 가 섞여 와도 된다. rawId 는 근거 묶음에 없는 id 라
    그대로 둔다.
    """

    expanded: dict[str, dict] = {}
    for ref in source_refs:
        if not isinstance(ref, (str, dict)):
            continue
        identifier = ref if isinstance(ref, str) else str(ref.get("rawId", ""))
        reason = ref.get("reason") if isinstance(ref, dict) else None
        refs = expand_evidence(clues, [identifier], request)
        for found in refs:
            dumped = found.model_dump(by_alias=True, mode="json")
            expanded.setdefault(found.raw_id, {**dumped, "reason": reason} if reason else dumped)
        if not refs and isinstance(ref, dict) and _is_uuid(identifier):
            expanded.setdefault(identifier, ref)
    return list(expanded.values())


def _is_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def _invalid_item_warning(kind: str, index: int) -> TimelineWarning:
    """스키마 검증에 실패해 제외한 항목을 알리는 warning."""

    return TimelineWarning(
        warning_id=f"warning-invalid-{kind}-{index:03d}",
        severity=TimelineWarningSeverity.MEDIUM,
        message=f"LLM 응답의 {kind} 항목({index}번)이 형식 검증에 실패해 제외했습니다.",
    )


def _report_skipped_item(kind: str, index: int, exc: Exception) -> None:
    """스키마 검증에 실패해 제외한 항목을 코드와 함께 남긴다.

    항목 하나가 깨져도 draft 전체는 살린다. 대신 어느 종류의 몇 번째 항목이 왜
    빠졌는지는 로그·관측에서 추적할 수 있어야 한다 — 그러지 않으면 LLM 이 특정
    필드를 계속 틀리고 있어도 결과만 조용히 빈약해진다.
    """

    report_error(
        logger,
        ErrorCode.STRUCTURED_OUTPUT_INVALID,
        "Timeline 응답 항목 검증 실패로 제외",
        exc=exc,
        context={"kind": kind, "index": index, "action": "skipped"},
        # 항목마다 부른다. 응답 하나가 통째로 깨지면 수십 건이 되므로 운영 이벤트로는
        # 내지 않는다. 결과가 비면 Timeline Agent 단계 실패가 그 사실을 말한다.
        emit=False,
    )


def parse_timeline_draft(text: str, request: TimelineDraftRequest) -> TimelineDraft:
    """LLM 응답을 ``TimelineDraft`` 로 파싱한다(부분 손상에 강하게).

    - JSON 객체를 찾지 못하거나 ``json.loads`` 에 실패하면 예외를 던져 상위
      fallback(빈 draft)로 넘긴다.
    - 개별 event/warning 이 스키마(ISO datetime·timezone·start/end 순서 등)
      검증에 실패하면 그 항목만 건너뛰고 warning 으로 남긴다. 한 항목의 손상이
      전체 draft 를 무너뜨리지 않게 한다.
    - ``clientEventId`` 는 검증을 통과한 event 순서대로 다시 부여하고,
      ``userId``/``date``/``timezone`` 은 LLM 값 대신 요청 기준값을 신뢰한다.
    - ``events``·``warnings`` 만 읽는다(:class:`TimelineAgentOutput`). 옛 프롬프트가
      내는 ``questions`` 같은 다른 키는 무시한다(#118).
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")

    payload = json.loads(text[start : end + 1])

    skipped: list[TimelineWarning] = []

    events: list[TimelineEventDraft] = []
    for index, raw in enumerate(payload.get("events") or [], start=1):
        if not isinstance(raw, dict):
            skipped.append(_invalid_item_warning("event", index))
            continue
        item = {**raw}
        item.pop("eventId", None)
        item["clientEventId"] = f"event-{len(events) + 1:03d}"
        try:
            event = TimelineEventDraft.model_validate(item)
            # 이 함수는 v1~v3 경로다(v4 는 `parse_timeline_narration`). 13종 밖의 값은 모르는
            # 문자열과 똑같이 그 항목만 뺀다 — draft 가 공용 enum 이라 검증을 통과한다(#172).
            if event.event_type not in PRE_CLUE_EVENT_TYPES:
                raise ValueError(f"unknown eventType: {event.event_type.value}")
            events.append(event)
        except (ValidationError, ValueError) as exc:
            _report_skipped_item("event", index, exc)
            skipped.append(_invalid_item_warning("event", index))

    warnings: list[TimelineWarning] = []
    for index, raw in enumerate(payload.get("warnings") or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = {**raw}
        item.setdefault("warningId", f"warning-{len(warnings) + 1:03d}")
        item.setdefault("severity", TimelineWarningSeverity.MEDIUM.value)
        try:
            warnings.append(TimelineWarning.model_validate(item))
        except ValidationError as exc:
            # 경고 자체가 깨지면 draft 에는 싣지 않는다(경고를 위한 경고는 소음이다).
            # 다만 어느 코드로 무엇이 버려졌는지는 추적할 수 있어야 한다.
            _report_skipped_item("warning", index, exc)

    return TimelineDraft(
        user_id=_DEFAULT_USER_ID,
        date=request.date,
        timezone=request.timezone or _DEFAULT_TIMEZONE,
        events=events,
        warnings=[*warnings, *skipped],
    )


class TimelineAgent(Agent):
    """Merge upstream event candidates into a daily timeline draft."""

    name = "timeline"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.TIMELINE)
        return self._llm

    def generate(
        self,
        request: TimelineDraftRequest,
        agent_result: AgentEventResult,
    ) -> TimelineDraft:
        """Return a schema-complete but **not yet repaired** ``TimelineDraft``.

        정렬·id 확정·겹침 정리는 main agent 의 repair node 가 이어서 수행한다.
        """

        carried = _carry_over_warnings(agent_result)
        try:
            return self._generate(request, agent_result, carried)
        except Exception as exc:
            failure_code = code_of_or(exc, ErrorCode.TIMELINE_AGENT_FAILED)
            report_error(
                logger,
                failure_code,
                "Timeline Agent 실행 실패",
                exc=exc,
                context={"fallback": "empty_draft"},
                exc_info=True,
            )
            return _empty_draft(
                request,
                warnings=[
                    *carried,
                    TimelineWarning(
                        warning_id="warning-timeline-001",
                        severity=TimelineWarningSeverity.HIGH,
                        message=(
                            f"{self.name} agent 실행 실패: "
                            f"{message_for(failure_code)}"
                        ),
                    ),
                ],
            )

    def generate_from_clues(
        self,
        request: TimelineDraftRequest,
        clues: TimelineClues,
    ) -> TimelineDraft:
        """v4 세트(#169, #202): 코드가 정한 event 에 글을 얹어 draft 를 만든다.

        실패 흡수는 `generate` 와 같다 — 빈 draft 를 돌려주고, 확정 pass 가 기본 event 를 틀 글로 채운다.
        """

        carried = [
            TimelineWarning(
                warning_id=f"warning-upstream-{index:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=f"[{warning.agent_name}] {warning.message}",
            )
            for index, warning in enumerate(clues.warnings, start=1)
        ]
        try:
            if not clues.bundles and not clues.day_notifications:
                return _empty_draft(request, warnings=carried)
            prompt = build_timeline_clue_prompt(
                request, user_memory_to_text(request.user_memory), clues
            )
            text = self.llm.complete_json(
                prompt, TimelineNarrationOutput, system=_SYSTEM_PROMPT, temperature=0.2
            )
            draft = parse_timeline_narration(text, request, clues)
            draft.warnings = [*carried, *draft.warnings]
            return draft
        except Exception as exc:
            failure_code = code_of_or(exc, ErrorCode.TIMELINE_AGENT_FAILED)
            report_error(
                logger,
                failure_code,
                "Timeline Agent 실행 실패",
                exc=exc,
                context={"fallback": "empty_draft", "contract": "clue"},
                exc_info=True,
            )
            return _empty_draft(
                request,
                warnings=[
                    *carried,
                    TimelineWarning(
                        warning_id="warning-timeline-001",
                        severity=TimelineWarningSeverity.HIGH,
                        message=f"{self.name} agent 실행 실패: {message_for(failure_code)}",
                    ),
                ],
            )

    def _generate(
        self,
        request: TimelineDraftRequest,
        agent_result: AgentEventResult,
        carried_warnings: list[TimelineWarning],
    ) -> TimelineDraft:
        if not agent_result.candidates and not agent_result.fragments:
            return _empty_draft(request, warnings=carried_warnings)

        prompt = build_timeline_prompt(
            request,
            user_memory_to_text(request.user_memory),
            items_to_text(agent_result.candidates),
            items_to_text(agent_result.fragments),
        )
        # 내부 draft 가 아니라 LLM 출력 계약을 스키마로 싣는다(#118). draft 전체를 실으면
        # 코드가 채우는 clientEventId·question 까지 모델에게 쓰라고 하는 셈이 된다.
        text = self.llm.complete_json(
            prompt, TimelineAgentOutput, system=_SYSTEM_PROMPT, temperature=0.2
        )
        draft = parse_timeline_draft(text, request)
        draft.warnings = [*carried_warnings, *draft.warnings]
        return draft


def _carry_over_warnings(agent_result: AgentEventResult) -> list[TimelineWarning]:
    """Convert upstream Event Agent warnings into TimelineDraft warnings."""

    return [
        TimelineWarning(
            warning_id=f"warning-upstream-{index:03d}",
            severity=TimelineWarningSeverity.MEDIUM,
            message=f"[{warning.agent_name}] {warning.message}",
        )
        for index, warning in enumerate(agent_result.warnings, start=1)
    ]


def _empty_draft(
    request: TimelineDraftRequest,
    *,
    warnings: list[TimelineWarning] | None = None,
) -> TimelineDraft:
    """Create an empty draft for no-input and failure paths."""

    return TimelineDraft(
        user_id=_DEFAULT_USER_ID,
        date=request.date,
        timezone=_DEFAULT_TIMEZONE,
        warnings=warnings or [],
    )

