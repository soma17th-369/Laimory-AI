"""사진 단서 Agent (v4 세트, #169).

v3 Photo Agent 는 vision 설명 뒤에 candidate(eventType·제목·문장·묶음)를 만들었다. v4 는
사진마다 **사용자가 그때 한 일** 한 문장만 읽어 낸다. 사진을 묶고 시간을 정하고 사건을
만드는 것은 Timeline v4 다.

    1. describe: v3 와 같은 vision 설명(`describer.py`). 바꾸지 않는다.
    2. interpret: 설명·촬영 시각·장소 후보로 사진마다 한 일 한 문장(`prompts/v4/prompt.md`).

해석을 vision 호출에 합치지 않는 것은 내 선택이다. 그래야 vision 프롬프트를 건드리지 않고,
trace 재실행이 기록된 설명으로 해석 단계만 다시 돌릴 수 있다.

사진은 사용자가 직접 고른 입력이라 **해석이 실패해도 사진을 버리지 않는다.** 그 사진은 해석
없는 단서로 남고 Timeline 이 설명만 보고 판단한다. 요청 window 밖에서 찍힌 사진은 그날의
사건이 될 수 없어 단서로 만들지 않는다(v3 Event Agent 의 window 강제와 같다).
"""

import json
from dataclasses import dataclass, field
from functools import cache

from app.agents.events.photo.describer import (
    PhotoDescriber,
    VisionPhotoDescriber,
    needs_description,
)
from app.agents.events.photo.image_source import default_photo_image_source
from app.agents.parsing import SupportsComplete, build_infer_prompt, default_llm
from app.agents.prompt_loader import load_prompt
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import code_of_or, report_error
from app.core.execution_context import ExecutionStage
from app.core.llm_stages import LLMStage
from app.core.logging import get_logger, stage_span
from app.schemas import AgentWarning, PhotoItem, TimelineDraftRequest
from app.schemas.clues import PhotoClue, PhotoInterpretationSet
from app.services.validator import parse_datetime, resolve_timezone, resolve_window_bounds

logger = get_logger(__name__)


@cache
def _system_prompt() -> str:
    return load_prompt(__file__, "prompt.md")


@dataclass
class PhotoClueResult:
    clues: list[PhotoClue] = field(default_factory=list)
    warnings: list[AgentWarning] = field(default_factory=list)


class PhotoClueAgent:
    """사진마다 사용자가 한 일을 읽어 사진 단서를 만든다."""

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
            self._llm = default_llm(LLMStage.PHOTO)
        return self._llm

    def generate(self, request: TimelineDraftRequest) -> PhotoClueResult:
        photos = _photos_in_window(request)
        if not photos:
            return PhotoClueResult()

        with stage_span(logger, ExecutionStage.EVENT_AGENT, agent=self.name) as outcome:
            described = self._describe(photos, request)
            clues = _clues(described, request)
            warnings: list[AgentWarning] = []
            try:
                interpretations = self._interpret(clues, request)
            except Exception as exc:
                failure_code = code_of_or(exc, ErrorCode.EVENT_AGENT_FAILED)
                report_error(
                    logger,
                    failure_code,
                    "사진 단서 해석 실패",
                    exc=exc,
                    context={"agent": self.name, "fallback": "description_only"},
                    exc_info=True,
                )
                warnings.append(
                    AgentWarning(
                        agent_name=self.name,
                        message=f"사진 해석 실패: {message_for(failure_code)}. 설명만으로 진행합니다.",
                    )
                )
                interpretations = {}
            for clue in clues:
                if (found := interpretations.get(clue.clue_id)) is not None:
                    clue.activity = found.activity.strip() or None
                    clue.uncertainty = list(found.uncertainty)
            missing = sum(1 for clue in clues if clue.activity is None)
            outcome["clueCount"] = len(clues)
            outcome["uninterpretedCount"] = missing
            return PhotoClueResult(clues=clues, warnings=warnings)

    def _describe(self, photos: list[PhotoItem], request: TimelineDraftRequest) -> list[PhotoItem]:
        describer = self._describer or VisionPhotoDescriber(
            image_source=default_photo_image_source(),
            llm=self._llm,
            stays=list(request.stays),
        )
        descriptions = describer.describe(photos)
        return [
            photo.model_copy(update={"description": descriptions[photo.raw_id]})
            if needs_description(photo) and photo.raw_id in descriptions
            else photo
            for photo in photos
        ]

    def _interpret(self, clues: list[PhotoClue], request: TimelineDraftRequest) -> dict:
        prompt = build_infer_prompt(
            json.dumps(
                [_prompt_entry(clue) for clue in clues], ensure_ascii=False, indent=2
            ),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        result = self.llm.complete_structured(
            prompt, PhotoInterpretationSet, system=_system_prompt(), temperature=0.2
        )
        return {item.photo_id: item for item in result.photos}


def _photos_in_window(request: TimelineDraftRequest) -> list[PhotoItem]:
    tz = resolve_timezone(request.timezone)
    bounds = resolve_window_bounds(request)
    photos = []
    for photo in request.photos:
        taken = parse_datetime(photo.taken_at, tz)
        if bounds is not None and taken is not None and not bounds.start <= taken <= bounds.end:
            continue
        photos.append(photo)
    return photos


def _clues(photos: list[PhotoItem], request: TimelineDraftRequest) -> list[PhotoClue]:
    """촬영 순서대로 `P1`, `P2` … 를 붙인다. 시각을 못 읽은 사진은 맨 뒤다."""

    tz = resolve_timezone(request.timezone)
    rows = [(parse_datetime(photo.taken_at, tz), photo) for photo in photos]
    rows.sort(key=lambda row: (row[0] is None, row[0].timestamp() if row[0] else 0.0))
    return [
        PhotoClue(
            clue_id=f"P{number}",
            raw_id=str(photo.raw_id),
            taken_at=taken,
            places=list(photo.places),
            address=photo.address,
            latitude=photo.latitude,
            longitude=photo.longitude,
            description=photo.description,
        )
        for number, (taken, photo) in enumerate(rows, start=1)
    ]


def _prompt_entry(clue: PhotoClue) -> dict:
    """해석 호출에 싣는 사진 한 장. rawId·좌표·URL 은 싣지 않는다(#80, #127)."""

    entry: dict = {"id": clue.clue_id}
    if clue.taken_at is not None:
        entry["takenAt"] = clue.taken_at.isoformat()
    if clue.places:
        entry["places"] = clue.places
    if clue.address:
        entry["address"] = clue.address
    entry["description"] = clue.description or None
    return entry
