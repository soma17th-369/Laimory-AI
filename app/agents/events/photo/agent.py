"""Photo Event Agent.

사진은 대체로 항목별 조각(fragment)으로 남기면 되므로 별도 graph 없이 **단일 LLM
호출**로 추론한다. 프롬프트는 같은 폴더의 `prompt.md`(system)에서 읽어 연결한다.
"""

from pathlib import Path

from app.agents.events.base_event_agent import EventAgent
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
    items_to_text,
    parse_agent_result,
    user_memory_to_text,
)
from app.schemas import AgentEventResult, TimelineDraftRequest

_SYSTEM_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


class PhotoEventAgent(EventAgent):
    """사진 source 를 해석하는 추론 Agent (단일 호출)."""

    name = "photo"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(getattr(request, "photos", None) or [])
        if not items:
            return AgentEventResult()

        infer_prompt = build_infer_prompt(
            user_memory_to_text(request.user_memory),
            items_to_text(items),
        )
        text = self.llm.complete(infer_prompt, system=_SYSTEM_PROMPT, temperature=0.2)
        return parse_agent_result(text)
