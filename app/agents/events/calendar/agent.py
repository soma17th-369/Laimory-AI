"""Calendar Event Agent.

캘린더 일정은 입력이 비교적 직접적이라 별도 graph 없이 **단일 LLM 호출**로
후보를 추론한다. system 프롬프트는 이 Agent의 `prompts/{PROMPT_VERSION}/prompt.md`
에서 읽어 연결한다.
"""

from app.agents.events.base_event_agent import EventAgent
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
    items_to_text,
    user_memory_to_text,
)
from app.agents.prompt_loader import load_prompt
from app.schemas import AgentEventResult, TimelineDraftRequest

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")


class CalendarEventAgent(EventAgent):
    """캘린더 source 를 해석하는 추론 Agent (단일 호출)."""

    name = "calendar"
    source_attrs = ("calendars",)

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(request.calendars)
        if not items:
            return AgentEventResult()

        infer_prompt = build_infer_prompt(
            user_memory_to_text(request.user_memory),
            items_to_text(items),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        return self.llm.complete_structured(
            infer_prompt, AgentEventResult, system=_SYSTEM_PROMPT, temperature=0.2
        )
