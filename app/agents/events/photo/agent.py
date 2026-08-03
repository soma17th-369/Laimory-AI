"""Photo Event Agent.

사진 metadata에서 event 후보와 후보보다 불확실한 하루 event 단서를 추출한다.

입력 사진의 `description` 은 보통 null 로 오므로, event 추론 전에 **describe →
infer** 2단계 agentic workflow(LangGraph)로 처리한다.

    1. describe: `PhotoDescriber` 로 description 이 비어 있는 사진들의 설명을
       배치 1회 호출로 채운다(`describer.py`).
    2. infer: 채워진 description 을 활동 근거로 삼아 event 후보를 추론한다.

프롬프트는 이 Agent의 `prompts/{PROMPT_VERSION}/` 아래 `prompt.md`(infer system)와
describe 프롬프트(describer 안에서 사용)에서 읽는다.
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.events.base_event_agent import EventAgent
from app.agents.events.photo.describer import (
    PhotoDescriber,
    VisionPhotoDescriber,
    needs_description,
)
from app.agents.events.photo.image_source import default_photo_image_source
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


class _State(TypedDict, total=False):
    photos: list
    result: AgentEventResult


class PhotoEventAgent(EventAgent):
    """사진 source를 해석하는 추론 Agent (describe → infer graph)."""

    name = "photo"

    def __init__(
        self,
        llm: SupportsComplete | None = None,
        describer: PhotoDescriber | None = None,
    ) -> None:
        self._llm = llm
        self._describer = describer

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    @property
    def describer(self) -> PhotoDescriber:
        # 기본 describer 는 실제 이미지를 vision 으로 보는 VisionPhotoDescriber 다.
        # 이미지 소스는 설정 기반(default_photo_image_source)이며, 이미지를 못 구한
        # 사진은 내부 fallback(메타데이터 기반)으로 자동 대체된다. agent 와 동일한
        # LLM 을 공유한다.
        if self._describer is None:
            self._describer = VisionPhotoDescriber(
                image_source=default_photo_image_source(),
                llm=self._llm,
            )
        return self._describer

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(getattr(request, "photos", None) or [])
        if not items:
            return AgentEventResult()
        return self._run_graph(items, request)

    def _run_graph(self, items: list, request: TimelineDraftRequest) -> AgentEventResult:
        describer = self.describer
        llm = self.llm

        def describe_node(state: _State) -> _State:
            photos = state["photos"]
            descriptions = describer.describe(photos)
            if not descriptions:
                return {"photos": photos}
            # description 이 비어 있던 사진에만 생성된 설명을 채워 넣는다(원본 불변).
            enriched = [
                photo.model_copy(update={"description": descriptions[photo.raw_id]})
                if needs_description(photo) and photo.raw_id in descriptions
                else photo
                for photo in photos
            ]
            return {"photos": enriched}

        def infer_node(state: _State) -> _State:
            photos = state["photos"]
            infer_prompt = build_infer_prompt(
                user_memory_to_text(request.user_memory),
                _photo_items_to_text(photos),
                date=request.date,
                window_start=request.window.start if request.window else None,
                window_end=request.window.end if request.window else None,
            )
            result = llm.complete_structured(
                infer_prompt, AgentEventResult, system=_SYSTEM_PROMPT, temperature=0.2
            )
            return {"result": result}

        graph = StateGraph(_State)
        graph.add_node("describe", describe_node)
        graph.add_node("infer", infer_node)
        graph.add_edge(START, "describe")
        graph.add_edge("describe", "infer")
        graph.add_edge("infer", END)
        return graph.compile().invoke({"photos": items})["result"]


def _photo_items_to_text(items: list) -> str:
    """사진 항목을 프롬프트용 JSON 으로 직렬화한다.

    파생 필드를 붙이지 않는다. 이전에는 `recommendedUse`/`timePolicy` 로 "독립 사진
    event 를 만들지 말고 STAY·CALENDAR 에 병합하라" 를 실었는데, 이 agent 의 입력은
    `request.photos` 뿐이라 STAY·CALENDAR 를 볼 수 없었다. 따를 수 없는 지시라
    자기 candidate 억제만 남았다. 병합 판단은 Timeline Agent 소관이다(#56).
    """

    return items_to_text(items)
