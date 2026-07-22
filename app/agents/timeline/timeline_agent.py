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
from pathlib import Path

from pydantic import ValidationError

from app.agents.base import Agent
from app.agents.parsing import (
    SupportsComplete,
    default_llm,
    items_to_text,
    user_memory_to_text,
)
from app.core.logging import get_logger
from app.core.observability import ObservationEventType, emit_observation
from app.schemas import (
    AgentEventResult,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineQuestion,
    TimelineWarning,
    TimelineWarningSeverity,
)
logger = get_logger(__name__)

_SYSTEM_PROMPT = (
    Path(__file__).parent / "timeline.md"
).read_text(encoding="utf-8")

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
        "unrelated activities; when sources conflict, use questions or warnings instead. "
        "Sort events by actual candidate time, and expose uncertain or missing context "
        "through questions, warnings, and uncertainty. "
        "Do not copy example dates; use the metadata date and the provided candidate times."
    )


def _invalid_item_warning(kind: str, index: int) -> TimelineWarning:
    """스키마 검증에 실패해 제외한 항목을 알리는 warning."""

    return TimelineWarning(
        warning_id=f"warning-invalid-{kind}-{index:03d}",
        severity=TimelineWarningSeverity.MEDIUM,
        message=f"LLM 응답의 {kind} 항목({index}번)이 형식 검증에 실패해 제외했습니다.",
    )


def parse_timeline_draft(text: str, request: TimelineDraftRequest) -> TimelineDraft:
    """LLM 응답을 ``TimelineDraft`` 로 파싱한다(부분 손상에 강하게).

    - JSON 객체를 찾지 못하거나 ``json.loads`` 에 실패하면 예외를 던져 상위
      fallback(빈 draft)로 넘긴다.
    - 개별 event/question/warning 이 스키마(ISO datetime·timezone·start/end 순서 등)
      검증에 실패하면 그 항목만 건너뛰고 warning 으로 남긴다. 한 항목의 손상이
      전체 draft 를 무너뜨리지 않게 한다.
    - ``clientEventId`` 는 검증을 통과한 event 순서대로 다시 부여하고,
      ``userId``/``date``/``timezone`` 은 LLM 값 대신 요청 기준값을 신뢰한다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM response did not contain a JSON object")

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
            events.append(TimelineEventDraft.model_validate(item))
        except ValidationError:
            skipped.append(_invalid_item_warning("event", index))

    questions: list[TimelineQuestion] = []
    for index, raw in enumerate(payload.get("questions") or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = {**raw}
        item.setdefault("questionId", f"question-{len(questions) + 1:03d}")
        try:
            questions.append(TimelineQuestion.model_validate(item))
        except ValidationError:
            skipped.append(_invalid_item_warning("question", index))

    warnings: list[TimelineWarning] = []
    for index, raw in enumerate(payload.get("warnings") or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = {**raw}
        item.setdefault("warningId", f"warning-{len(warnings) + 1:03d}")
        item.setdefault("severity", TimelineWarningSeverity.MEDIUM.value)
        try:
            warnings.append(TimelineWarning.model_validate(item))
        except ValidationError:
            continue  # 경고 자체가 깨지면 조용히 버린다

    return TimelineDraft(
        user_id=_DEFAULT_USER_ID,
        date=request.date,
        timezone=request.timezone or _DEFAULT_TIMEZONE,
        events=events,
        questions=questions,
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
            self._llm = default_llm()
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
            logger.warning("Timeline Agent failed: error=%s", exc, exc_info=True)
            emit_observation(
                ObservationEventType.FAILED,
                payload={"error": str(exc), "fallback": "empty_draft"},
            )
            return _empty_draft(
                request,
                warnings=[
                    *carried,
                    TimelineWarning(
                        warning_id="warning-timeline-001",
                        severity=TimelineWarningSeverity.HIGH,
                        message=f"{self.name} agent 실행 실패: {exc}",
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
        text = self.llm.complete(prompt, system=_SYSTEM_PROMPT, temperature=0.2)
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

