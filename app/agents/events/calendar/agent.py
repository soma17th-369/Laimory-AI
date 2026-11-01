"""Calendar Event Agent.

캘린더 일정은 입력이 비교적 직접적이라 별도 graph 없이 **단일 LLM 호출**로
후보를 추론한다. system 프롬프트는 이 Agent의 `prompts/{PROMPT_VERSION}/prompt.md`
에서 읽어 연결한다.
"""

from functools import cache

from app.agents.events.base_event_agent import EventAgent
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
    items_to_text,
)
from app.agents.prompt_loader import load_prompt
from app.schemas import AgentEventResult, TimelineDraftRequest
from app.core.llm_stages import LLMStage

#: 프롬프트는 import 때가 아니라 처음 부를 때 읽는다. v4 세트에는 이 Agent 의
#: 프롬프트가 없다 — v4 는 일정 단서 코드가 대신한다(#168). import 때 읽으면 v4 로
#: 기동조차 못 한다. v1~v3 은 읽는 시점만 늦어질 뿐 같은 파일을 읽는다.
@cache
def _system_prompt() -> str:
    return load_prompt(__file__, "prompt.md")


class CalendarEventAgent(EventAgent):
    """캘린더 source 를 해석하는 추론 Agent (단일 호출)."""

    name = "calendar"
    source_attrs = ("calendars",)

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.CALENDAR)
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(request.calendars)
        if not items:
            return AgentEventResult()

        infer_prompt = build_infer_prompt(
            items_to_text(items),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        return self.llm.complete_structured(
            infer_prompt, AgentEventResult, system=_system_prompt(), temperature=0.2
        )
