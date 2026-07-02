"""Timeline Agent.

This agent merges ``AiEventCandidate`` values produced by data-specific Event
Agents into a user-editable ``TimelineDraft``. The LLM decides the semantic
merge, while code owns deterministic draft ids and fallback warnings.
"""

import json
from pathlib import Path

from app.agents.base import Agent
from app.agents.parsing import (
    SupportsComplete,
    default_llm,
    items_to_text,
    user_memory_to_text,
)
from app.core.logging import get_logger
from app.schemas import (
    AgentEventResult,
    TimelineDraft,
    TimelineDraftRequest,
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

    return (
        "[draft metadata]\n"
        f"userId: {_DEFAULT_USER_ID}\n"
        f"date: {request.date}\n"
        f"timezone: {_DEFAULT_TIMEZONE}\n\n"
        f"[user memory]\n{user_memory_text}\n\n"
        f"[AI Event candidates]\n{candidates_text}\n\n"
        f"[Source fragments]\n{fragments_text}\n\n"
        "Sort and merge candidates into the TimelineDraft JSON format. "
        "Use candidates as the primary evidence and fragments only as supporting evidence."
    )


def parse_timeline_draft(text: str) -> TimelineDraft:
    """Parse an LLM response into ``TimelineDraft`` and assign stable ids.

    The LLM may omit ids or hallucinate ids. Event ids are always rewritten as
    ``event-001`` style ``clientEventId`` values in event order. Missing
    question/warning ids are also filled deterministically.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM response did not contain a JSON object")

    payload = json.loads(text[start : end + 1])

    for index, event in enumerate(payload.get("events") or [], start=1):
        event.pop("eventId", None)
        event["clientEventId"] = f"event-{index:03d}"

    for index, question in enumerate(payload.get("questions") or [], start=1):
        question.setdefault("questionId", f"question-{index:03d}")

    for index, warning in enumerate(payload.get("warnings") or [], start=1):
        warning.setdefault("warningId", f"warning-{index:03d}")
        warning.setdefault("severity", TimelineWarningSeverity.MEDIUM.value)

    return TimelineDraft.model_validate(payload)


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
        """Return a schema-complete ``TimelineDraft``."""

        carried = _carry_over_warnings(agent_result)
        try:
            return self._generate(request, agent_result, carried)
        except Exception as exc:
            logger.warning("Timeline Agent failed: error=%s", exc, exc_info=True)
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
        draft = parse_timeline_draft(text)
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
