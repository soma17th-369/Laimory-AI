"""Location Event Agent.

위치 체류/이동은 여러 건을 조합하고 과한 추론을 걸러야 하므로 **infer → review**
2단계 LangGraph 를 이 agent 안에서 직접 구성한다. 프롬프트는 같은 폴더의
`prompt.md`(system) 과 `review.md`(검토)에서 읽어 연결한다.
"""

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

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

_DIR = Path(__file__).parent
_SYSTEM_PROMPT = (_DIR / "prompt.md").read_text(encoding="utf-8")
_REVIEW_PROMPT = (_DIR / "review.md").read_text(encoding="utf-8")


class _State(TypedDict, total=False):
    draft: str
    final: str


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
        final = self._run_graph(infer_prompt)
        return parse_agent_result(final)

    def _run_graph(self, infer_prompt: str) -> str:
        llm = self.llm

        def infer_node(state: _State) -> _State:
            draft = llm.complete(infer_prompt, system=_SYSTEM_PROMPT, temperature=0.2)
            return {"draft": draft}

        def review_node(state: _State) -> _State:
            prompt = _REVIEW_PROMPT.replace("{{DRAFT}}", state["draft"])
            final = llm.complete(prompt, system=_SYSTEM_PROMPT, temperature=0.0)
            return {"final": final}

        graph = StateGraph(_State)
        graph.add_node("infer", infer_node)
        graph.add_node("review", review_node)
        graph.add_edge(START, "infer")
        graph.add_edge("infer", "review")
        graph.add_edge("review", END)
        return graph.compile().invoke({})["final"]
