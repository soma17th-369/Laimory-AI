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
from datetime import timedelta
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
    PRE_CLUE_EVENT_TYPES,
    TimelineAgentOutput,
    TimelineClueEvent,
    TimelineClueOutput,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.clue_bundles import (
    TimelineClues,
    expand_evidence,
    misplaced_clues,
    timeline_input_sections,
)

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "timeline.md")

_DEFAULT_USER_ID = "user-1234"
_DEFAULT_TIMEZONE = "Asia/Seoul"


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
    """v4 세트의 user prompt(#169). 입력은 Event Agent candidate 대신 근거 묶음이다."""

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
        f"[근거 묶음]\n{dump(sections['bundles'])}\n\n"
        f"[빈 시간]\n{dump(sections['emptyTimes'])}\n\n"
        f"[알림]\n{dump(sections['notifications'])}\n\n"
        f"[user memory]\n{user_memory_text}\n\n"
        "위 근거 묶음으로 이날의 하루를 TimelineDraft JSON 으로 옮겨 적으세요. "
        "근거는 묶음·단서 id 로 가리키고, 예시의 날짜를 옮기지 말고 metadata 의 날짜와 "
        "묶음의 시각을 쓰세요."
    )


def parse_timeline_clue_draft(
    text: str, request: TimelineDraftRequest, clues: TimelineClues
) -> TimelineDraft:
    """v4 LLM 응답을 draft 로 파싱하고 근거 id 를 rawId 로 펼친다(#169).

    v3 의 `parse_timeline_draft` 와 같이 부분 손상에 강하다. 깨진 event 는 그것만 건너뛴다.
    근거 id 가 하나도 입력의 rawId 로 펼쳐지지 않은 event 도 건너뛴다 — 근거 없는 event 다.
    장소는 비워 둔다. 확정 pass 가 근거로 정한다(#171).
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
    payload = json.loads(text[start : end + 1])

    skipped: list[TimelineWarning] = []
    parsed: list[TimelineClueEvent] = []
    for index, raw in enumerate(payload.get("events") or [], start=1):
        if not isinstance(raw, dict):
            skipped.append(_invalid_item_warning("event", index))
            continue
        try:
            parsed.append(TimelineClueEvent.model_validate(raw))
        except ValidationError as exc:
            _report_skipped_item("event", index, exc)
            skipped.append(_invalid_item_warning("event", index))

    # 기본 event 는 그 event 의 시간과 겹칠 때만 출처다. 실제 Timeline 은 오전 11:24 작업 사진 event 에
    # 16:59 체류의 `E1` 을 적었고, 그 체류 기록이 근거로 붙자 길이 0 복원이 event 를 16:59 로 옮겨
    # 사진이 떨어져 나갔다. 겹치지 않는 출처는 시간 판단이 아니라 잘못 적은 것이라 버린다.
    segments = {b.base_event.base_id: b.base_event.segment for b in clues.bundles if b.base_event}
    edge = timedelta(minutes=1)
    bases_by_event = [
        [
            base
            for base in event.base_event_ids
            if (segment := segments.get(base.strip())) is not None
            and segment.start - edge <= event.end_time
            and event.start_time <= segment.end + edge
        ]
        for event in parsed
    ]
    # `from` 을 비운 사진 event 는 그 사진이 붙은 위치 구간에서 온 것이다(사용자 결정 2026-10-31 — 단서를
    # 위치에 잇는 것은 코드가 한다). 사진 입력에서 장소를 뺀 뒤, Timeline 이 꺼낸 식사의 `from` 을 비우자
    # 그 식사가 장소도 지명도 없는 `점심을 먹었다` 가 됐다. 사진이 붙은 구간 중 event 시간과 겹치는 것만 쓴다.
    photo_bases = {
        photo.clue_id: [b.base_event.base_id for b in clues.bundles if b.base_event and photo in b.photos]
        for photo in clues.photos
    }
    for event, bases in zip(parsed, bases_by_event):
        if bases:
            continue
        bases.extend(
            base
            for base in dict.fromkeys(
                base for clue_id in event.evidence for base in photo_bases.get(clue_id.strip(), [])
            )
            if segments[base].start - edge <= event.end_time
            and event.start_time <= segments[base].end + edge
        )
    refs_by_event = expand_evidence(
        clues, [event.evidence for event in parsed], request, bases_by_event
    )
    events: list[TimelineEventDraft] = []
    for index, (event, refs, bases) in enumerate(
        zip(parsed, refs_by_event, bases_by_event), start=1
    ):
        # 시간·장소가 맞지 않는 사진·일정은 그 event 의 근거가 아니다(사용자 결정 2026-10-30·31).
        misplaced = misplaced_clues(clues, event.start_time, event.end_time, refs)
        if misplaced:
            refs = [ref for ref in refs if ref.raw_id not in misplaced]
            skipped.append(
                TimelineWarning(
                    warning_id=f"warning-misplaced-evidence-{index:03d}",
                    severity=TimelineWarningSeverity.LOW,
                    message=(
                        f"LLM 응답의 event 항목({index}번)이 시간·장소가 맞지 않는 근거 "
                        f"{', '.join(sorted(misplaced.values()))} 를 적어 뺐습니다."
                    ),
                )
            )
        if not refs:
            skipped.append(
                TimelineWarning(
                    warning_id=f"warning-invalid-evidence-{index:03d}",
                    severity=TimelineWarningSeverity.MEDIUM,
                    message=(
                        f"LLM 응답의 event 항목({index}번)이 입력에 없는 근거만 가리켜 "
                        "제외했습니다."
                    ),
                )
            )
            continue
        events.append(
            TimelineEventDraft(
                client_event_id=f"event-{len(events) + 1:03d}",
                event_type=event.event_type,
                title=event.title,
                description=event.description,
                start_time=event.start_time,
                end_time=event.end_time,
                confidence=event.confidence,
                inference_level=event.inference_level,
                source_refs=refs,
                uncertainty=list(event.uncertainty),
                place_candidates=[n.strip() for n in event.place_candidates if n.strip()],
                merge_reason=event.merge_reason.strip() or None,
                base_event_ids=[base.strip() for base in bases],
            )
        )

    warnings: list[TimelineWarning] = []
    for index, raw in enumerate(payload.get("warnings") or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = {**raw}
        item.setdefault("warningId", f"warning-{len(warnings) + 1:03d}")
        item.setdefault("severity", TimelineWarningSeverity.MEDIUM.value)
        # v4 는 근거를 묶음·단서 id 로 가리킨다. warning 에 그 id 를 적으면 rawId 로 펼친다 —
        # 그대로 두면 rawId 검증에 걸려 warning 이 통째로 버려진다(재실행에서 본 것).
        identifiers = [
            ref if isinstance(ref, str) else str(ref.get("rawId", ""))
            for ref in item.get("sourceRefs") or []
            if isinstance(ref, (str, dict))
        ]
        if identifiers:
            # 단서 id 와 rawId 가 섞여 와도 된다. rawId 는 근거 묶음에 없는 id 라 그대로 둔다.
            [refs] = expand_evidence(clues, [identifiers], request)
            kept = {ref.raw_id for ref in refs}
            item["sourceRefs"] = [ref.model_dump(by_alias=True, mode="json") for ref in refs] + [
                ref
                for ref in item.get("sourceRefs") or []
                if isinstance(ref, dict) and ref.get("rawId") not in kept and _is_uuid(ref.get("rawId"))
            ]
        try:
            warnings.append(TimelineWarning.model_validate(item))
        except ValidationError as exc:
            _report_skipped_item("warning", index, exc)

    return TimelineDraft(
        user_id=_DEFAULT_USER_ID,
        date=request.date,
        timezone=request.timezone or _DEFAULT_TIMEZONE,
        events=events,
        warnings=[*warnings, *skipped],
    )


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
            # 이 함수는 v1~v3 경로다(v4 는 `parse_timeline_clue_draft`). 13종 밖의 값은 모르는
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
        """v4 세트(#169): 근거 묶음으로 draft 를 만든다. 실패 흡수는 `generate` 와 같다."""

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
                prompt, TimelineClueOutput, system=_SYSTEM_PROMPT, temperature=0.2
            )
            draft = parse_timeline_clue_draft(text, request, clues)
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

