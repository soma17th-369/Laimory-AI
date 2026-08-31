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
    items_to_text_without_coordinates,
)
from app.agents.prompt_loader import load_prompt
from app.core.langfuse_tracing import trace_observation, update_observation
from app.core.logging import get_logger, log_fields
from app.core.llm_stages import LLMStage
from app.schemas import AgentEventResult, TimelineDraftRequest

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")


class _State(TypedDict, total=False):
    photos: list
    result: AgentEventResult


class PhotoEventAgent(EventAgent):
    """사진 source를 해석하는 추론 Agent (describe → infer graph)."""

    name = "photo"
    source_attrs = ("photos",)

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
            self._llm = default_llm(LLMStage.PHOTO)
        return self._llm

    def _build_describer(self, request: TimelineDraftRequest) -> PhotoDescriber:
        """기본 describer 는 실제 이미지를 vision 으로 보는 `VisionPhotoDescriber` 다.

        이미지 소스는 설정 기반(`default_photo_image_source`)이며, 이미지를 못 구한
        사진은 메타데이터 fallback 으로 자동 대체된다. fallback 이 촬영 시각의 장소를
        적을 수 있도록 요청의 STAY 를 넘긴다(#56 §12).
        """

        if self._describer is not None:
            return self._describer
        return VisionPhotoDescriber(
            image_source=default_photo_image_source(),
            llm=self._llm,
            stays=list(request.stays),
        )

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(getattr(request, "photos", None) or [])
        if not items:
            return AgentEventResult()
        return self._run_graph(items, request)

    def _run_graph(self, items: list, request: TimelineDraftRequest) -> AgentEventResult:
        describer = self._build_describer(request)
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
                _photo_items_to_text(photos),
                date=request.date,
                window_start=request.window.start if request.window else None,
                window_end=request.window.end if request.window else None,
            )
            result = llm.complete_structured(
                infer_prompt, AgentEventResult, system=_SYSTEM_PROMPT, temperature=0.2
            )
            _observe_photo_counts(items, photos, result)
            return {"result": result}

        graph = StateGraph(_State)
        graph.add_node("describe", describe_node)
        graph.add_node("infer", infer_node)
        graph.add_edge(START, "describe")
        graph.add_edge("describe", "infer")
        graph.add_edge("infer", END)
        return graph.compile().invoke({"photos": items})["result"]


def _observe_photo_counts(
    selected: list, described: list, result: AgentEventResult
) -> None:
    """사진이 단계마다 몇 장씩 살아남았는지 남긴다 (#56 §7.3).

    사진이 최종 타임라인에서 사라졌을 때 **어느 단계에서** 빠졌는지 알 수 있어야 한다.
    선택 → 설명 생성 → candidate·fragment 로 이어지는 개수를 한 줄에 모아 둔다.

    운영 이벤트(#53)에는 넣지 않는다. 수집 대상은 allowlist 로 고정돼 있고, 이건 AI 실행
    관측이라 Langfuse 쪽이다. rawId 는 어느 쪽에도 남기지 않는다.
    """

    covered = {
        str(ref.raw_id)
        for candidate in result.candidates
        for ref in candidate.source_refs
    } | {str(fragment.raw_id) for fragment in result.fragments}

    described_count = sum(1 for photo in described if not needs_description(photo))
    covered_count = sum(1 for photo in described if str(photo.raw_id) in covered)
    counts = {
        "photoSelectedCount": len(selected),
        "photoDescribedCount": described_count,
        "photoAgentInputCount": len(described),
        "photoInCandidateOrFragmentCount": covered_count,
        "candidateCount": len(result.candidates),
        "fragmentCount": len(result.fragments),
    }
    with trace_observation(
        "photo-stage-counts", as_type="span", metadata=counts
    ) as observation:
        # 선택한 사진이 candidate·fragment 어디에도 못 들어갔으면 관측에서 눈에 띄어야 한다.
        update_observation(
            observation,
            level="WARNING" if covered_count < len(described) else None,
        )
    logger.debug("사진 단계별 개수", extra=log_fields(**counts))


def _photo_items_to_text(items: list) -> str:
    """사진 항목을 프롬프트용 JSON 으로 직렬화한다.

    파생 필드를 붙이지 않는다. 이전에는 `recommendedUse`/`timePolicy` 로 "독립 사진
    event 를 만들지 말고 STAY·CALENDAR 에 병합하라" 를 실었는데, 이 agent 의 입력은
    `request.photos` 뿐이라 STAY·CALENDAR 를 볼 수 없었다. 따를 수 없는 지시라
    자기 candidate 억제만 남았다. 병합 판단은 Timeline Agent 소관이다(#56).

    좌표는 싣지 않는다(#80). 사진의 위치는 `place`/`places`/`address` 로 오고, 그것이 없으면
    촬영 시각으로 STAY 를 대조하는 기존 경로가 답한다.
    """

    return items_to_text_without_coordinates(items)
