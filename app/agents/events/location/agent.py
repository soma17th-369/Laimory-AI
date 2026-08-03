"""Location Event Agent.

v1 은 **infer → review** 2단계 LangGraph 다. 자유 텍스트로 추론한 뒤 review 노드가
구조화하면서 과한 추론을 걸렀다.

v2 부터는 review 를 두지 않는다(#56). v2 의 목표가 상위 여정 복원·공백 구간 추론처럼
**적극적인 추론**이라 억제를 전제한 review 와 방향이 반대이고, 남은 검증은 `§4.4`
검증 코드와 Repair Agent 가 맡는다. review 를 빼면 `infer` 가 직접 구조화해야 하므로
`complete_structured` 로 부른다.

분기 기준은 프롬프트 버전이다. `PROMPT_VERSION=v1` 롤백이 2단계 동작까지 그대로
되돌리도록 하기 위해서다. 문자열 대소 비교(`> "v1"`)는 `"v10" > "v9"` 가 거짓이라
쓰지 않는다.
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.events.base_event_agent import EventAgent
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
    items_to_text,
    user_memory_to_text,
)
from app.agents.prompt_loader import load_prompt
from app.core.config import settings
from app.schemas import AgentEventResult, TimelineDraftRequest

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")

#: review 단계는 v1 프롬프트 세트 전용이다. v2 세트에는 `review.md` 가 없다.
_USE_REVIEW = settings.prompt_version == "v1"
_REVIEW_PROMPT = load_prompt(__file__, "review.md") if _USE_REVIEW else None


class _State(TypedDict, total=False):
    draft: str
    result: AgentEventResult


class LocationEventAgent(EventAgent):
    """위치 source 를 해석하는 추론 Agent (infer → review graph)."""

    name = "location"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = [*request.stays, *request.movements]
        if not items:
            return AgentEventResult()

        infer_prompt = build_infer_prompt(
            user_memory_to_text(request.user_memory),
            items_to_text(items),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        if _REVIEW_PROMPT is None:
            return self.llm.complete_structured(
                infer_prompt,
                AgentEventResult,
                system=_SYSTEM_PROMPT,
                temperature=0.2,
            )
        return self._run_graph(infer_prompt)

    def _run_graph(self, infer_prompt: str) -> AgentEventResult:
        llm = self.llm

        def infer_node(state: _State) -> _State:
            draft = llm.complete(infer_prompt, system=_SYSTEM_PROMPT, temperature=0.2)
            return {"draft": draft}

        def review_node(state: _State) -> _State:
            prompt = _REVIEW_PROMPT.replace("{{DRAFT}}", state["draft"])
            result = llm.complete_structured(
                prompt, AgentEventResult, system=_SYSTEM_PROMPT, temperature=0.0
            )
            return {"result": result}

        graph = StateGraph(_State)
        graph.add_node("infer", infer_node)
        graph.add_node("review", review_node)
        graph.add_edge(START, "infer")
        graph.add_edge("infer", "review")
        graph.add_edge("review", END)
        return graph.compile().invoke({})["result"]
