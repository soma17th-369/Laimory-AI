"""Photo Event Agent.

사진 metadata에서 event 후보와 후보보다 불확실한 하루 event 단서를 추출한다.

입력 사진의 `description` 은 보통 null 로 오므로, event 추론 전에 **describe →
infer** 2단계 agentic workflow(LangGraph)로 처리한다.

    1. describe: `PhotoDescriber` 로 description 이 비어 있는 사진들의 설명을
       배치 1회 호출로 채운다(`describer.py`).
    2. infer: 채워진 description 을 활동 근거로 삼아 event 후보를 추론한다.

프롬프트는 같은 폴더의 `prompt.md`(infer system) 과 `describe_prompt.md`
(describe system, describer 안에서 사용)에서 읽는다.
"""

import json
from pathlib import Path
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
    parse_agent_result,
    user_memory_to_text,
)
from app.schemas import AgentEventResult, TimelineDraftRequest

_SYSTEM_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


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
                photo.model_copy(update={"description": descriptions[photo.id]})
                if needs_description(photo) and photo.id in descriptions
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
            text = llm.complete(infer_prompt, system=_SYSTEM_PROMPT, temperature=0.2)
            return {"result": parse_agent_result(text)}

        graph = StateGraph(_State)
        graph.add_node("describe", describe_node)
        graph.add_node("infer", infer_node)
        graph.add_edge(START, "describe")
        graph.add_edge("describe", "infer")
        graph.add_edge("infer", END)
        return graph.compile().invoke({"photos": items})["result"]


def _photo_items_to_text(items: list) -> str:
    payload = [_enrich_photo_item(item) for item in items]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _enrich_photo_item(item) -> dict:
    payload = item.model_dump(by_alias=True, mode="json")
    description = (getattr(item, "description", None) or "").strip()
    payload["recommendedUse"] = "MERGE_WITH_STAY_OR_CALENDAR"
    payload["photoMeaning"] = {
        "description": description,
        "useForActivityInference": bool(description),
    }
    payload["timePolicy"] = (
        "Input excludes downloaded images. takenAt/startAt is the actual shooting time. "
        "Prefer using the photo as supporting evidence merged with STAY or CALENDAR "
        "instead of creating a standalone photo-taking event."
    )
    return payload
