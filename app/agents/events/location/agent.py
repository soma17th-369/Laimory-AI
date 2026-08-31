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

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.events.base_event_agent import EventAgent
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
    items_to_text_without_coordinates,
)
from app.agents.prompt_loader import load_prompt
from app.core.config import settings
from app.core.llm_stages import LLMStage
from app.schemas import AgentEventResult, TimelineDraftRequest
from app.services.location_guard import verify_location_result
from app.services.location_metrics import build_location_metrics

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
    source_attrs = ("stays", "movements")

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.LOCATION)
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = [*request.stays, *request.movements]
        if not items:
            return AgentEventResult()

        infer_prompt = build_infer_prompt(
            _location_data_text(request, items),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        if _REVIEW_PROMPT is None:
            result = self.llm.complete_structured(
                infer_prompt,
                AgentEventResult,
                system=_SYSTEM_PROMPT,
                temperature=0.2,
            )
        else:
            result = self._run_graph(infer_prompt)
        return verify_location_result(result, request)

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


def _location_data_text(request: TimelineDraftRequest, items: list) -> str:
    """raw 항목 + 코드가 계산한 파생 지표를 함께 직렬화한다.

    프롬프트가 평균 속도·구간 공백·마지막 관측 같은 값을 근거로 판단하라고 하는데,
    입력 DTO 에는 그 필드가 없다. LLM 이 매 구간 암산하는 대신 코드가 계산해 준다(#56 §4.4).
    raw 항목의 형태는 그대로 두고 파생값을 별도 블록으로 덧붙인다.

    좌표는 프롬프트에 싣지 않는다(#80). request 로는 계속 받고 코드가 파생 지표 계산에 쓴다.
    """

    payload = json.loads(items_to_text_without_coordinates(items))
    metrics = build_location_metrics(request).as_prompt_dict()
    return json.dumps(
        {"locationItems": payload, "derivedMetrics": metrics},
        ensure_ascii=False,
        indent=2,
    )
