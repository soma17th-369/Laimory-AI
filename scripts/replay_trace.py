"""Langfuse trace 재실행 도구 (#167~#171).

운영 trace 내보내기 파일(`observations` 가 든 JSON)을 받아 두 가지를 한다.

    # LLM 없이: v4 위치 뼈대·근거 묶음과, 같은 입력에 기록된 v3 Location 결과를 나란히 본다.
    uv run python scripts/replay_trace.py skeleton data/trace/trace-*.json

    # 실제 LLM 으로: 기록된 입력과 사진 설명으로 파이프라인 전체를 한 번 돌린다.
    # 버전은 PROMPT_VERSION 으로 고른다(모듈이 import 때 프롬프트를 읽으므로 한 실행에 한 버전).
    PROMPT_VERSION=v4 uv run python scripts/replay_trace.py run data/trace/trace-*.json --out <dir>

사진은 다시 내려받지 않는다. v3 는 기록된 `event-agent-photo` 결과를, v4 는 기록된
`describe-photo-images` 설명을 그대로 쓴다 — 운영에서 본 사진 해석을 믿고 나머지 단계만 다시
돈다. User Memory 는 trace 에서 본문이 가려져 있어 싣지 않는다. 질문 단계는 품질 기준에
들지 않아 돌리지 않는다. trace 데이터와 결과는 커밋하지 않는다.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

os.environ.setdefault("LANGFUSE_ENABLED", "false")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.events import default_event_agents  # noqa: E402
from app.agents.events.base_event_agent import EventAgent  # noqa: E402
from app.agents.events.photo.clue_agent import PhotoClueAgent  # noqa: E402
from app.agents.events.photo.describer import PhotoDescriber  # noqa: E402
from app.agents.main import run_main_agent  # noqa: E402
from app.agents.question import QuestionAgent  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.langfuse_tracing import token_usage_scope  # noqa: E402
from app.schemas import (  # noqa: E402
    AgentEventResult,
    CollectedSnapshot,
    TimelineDraft,
    TimelineDraftRequest,
)
from app.services.calendar_clues import build_calendar_clues  # noqa: E402
from app.services.clue_bundles import build_timeline_clues  # noqa: E402
from app.services.location_skeleton import build_location_skeleton  # noqa: E402
from app.services.normalizer import normalize  # noqa: E402
from app.services.place_slots import filled_description, place_names_in_title  # noqa: E402
from app.services.timeline_result import build_result_request  # noqa: E402


# --- trace 읽기 ------------------------------------------------------------------


def _observations(trace: dict, name: str) -> list[dict]:
    return [o for o in trace.get("observations", []) if o.get("name") == name]


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def load_request(trace: dict) -> TimelineDraftRequest:
    [observation] = _observations(trace, "retrieve-source-snapshot")[:1]
    snapshot = dict(_json(observation["output"])["snapshot"])
    snapshot.pop("userMemory", None)
    return normalize(CollectedSnapshot.model_validate(snapshot))


def recorded_descriptions(trace: dict) -> dict[str, str]:
    """vision 이 쓴 사진 설명. 응답이 여러 번에 나뉘어 기록될 수 있어 모두 모은다."""

    found: dict[str, str] = {}
    for observation in _observations(trace, "describe-photo-images"):
        output = _json(observation.get("output"))
        messages = output if isinstance(output, list) else [output]
        for message in messages:
            content = _json(message.get("content")) if isinstance(message, dict) else None
            for entry in (content or {}).get("descriptions", []) if isinstance(content, dict) else []:
                if entry.get("rawId") and entry.get("description"):
                    found[str(entry["rawId"])] = entry["description"]
    return found


def recorded_photo_result(trace: dict) -> AgentEventResult:
    observations = _observations(trace, "event-agent-photo")
    if not observations:
        return AgentEventResult()
    return AgentEventResult.model_validate(_json(observations[0]["output"])["result"])


def recorded_location_result(trace: dict) -> AgentEventResult:
    observations = _observations(trace, "event-agent-location")
    if not observations:
        return AgentEventResult()
    return AgentEventResult.model_validate(_json(observations[0]["output"])["result"])


class RecordedDescriber(PhotoDescriber):
    def __init__(self, descriptions: dict[str, str]) -> None:
        self._descriptions = descriptions

    def describe(self, photos):
        return {p.raw_id: self._descriptions[p.raw_id] for p in photos if p.raw_id in self._descriptions}


class RecordedPhotoAgent(EventAgent):
    name = "photo"
    source_attrs = ("photos",)

    def __init__(self, result: AgentEventResult) -> None:
        self._result = result

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        return self._result.model_copy(deep=True)


class _SilentQuestionAgent(QuestionAgent):
    def generate(self, request, draft):
        return draft


# --- 뼈대 대조 (LLM 없음) -----------------------------------------------------------


def _hhmm(value) -> str:
    return value.strftime("%H:%M") if value else "--:--"


def skeleton_report(trace: dict) -> str:
    request = load_request(trace)
    skeleton = build_location_skeleton(request)
    clues = build_timeline_clues(skeleton, build_calendar_clues(request), [], [])
    lines = [f"위치 기록 {len(request.stays)}체류·{len(request.movements)}이동, hasLocation={skeleton.has_location}"]
    lines.append("## v4 뼈대")
    for segment in skeleton.segments:
        label = segment.spot.label if segment.spot else None
        lines.append(
            f"- {segment.segment_id} {segment.kind.value:8} {_hhmm(segment.start)}~{_hhmm(segment.end)} "
            f"기록 {len(segment.records)} {label or ''}"
            + (" 왕복" if segment.round_trip else "")
            + (f" 공백 {len(segment.internal_gaps)}" if segment.internal_gaps else "")
        )
    for empty in skeleton.empty_times:
        lines.append(f"- 빈 시간 {empty.kind.value} {_hhmm(empty.start)}~{_hhmm(empty.end)} samePlace={empty.same_place}")
    for record in skeleton.isolated:
        lines.append(f"- 단독 기록 {record.kind} {_hhmm(record.start)}~{_hhmm(record.end)}")
    lines.append(f"- 묶음 {len(clues.bundles)}개")

    recorded = recorded_location_result(trace)
    lines.append("## 기록된 v3 Location")
    owners = {raw_id: s.segment_id for s in skeleton.segments for raw_id in s.raw_ids}
    for candidate in recorded.candidates:
        raw_ids = [ref.raw_id for ref in candidate.source_refs]
        segments = sorted({owners.get(r, "?") for r in raw_ids})
        lines.append(
            f"- {candidate.event_type.value:8} {_hhmm(candidate.time_range.start_time)}~"
            f"{_hhmm(candidate.time_range.end_time)} {candidate.title} | rawId {len(raw_ids)} → {','.join(segments)}"
        )
    for fragment in recorded.fragments:
        lines.append(f"- fragment {fragment.raw_id[:8]} → {owners.get(fragment.raw_id, '?')}")
    return "\n".join(lines)


# --- 실제 LLM 재실행 ----------------------------------------------------------------


async def replay(trace: dict) -> dict:
    request = load_request(trace)
    version = settings.prompt_version
    started = perf_counter()
    with token_usage_scope() as usage:
        if version == "v4":
            draft = await run_main_agent(
                request,
                question_agent=_SilentQuestionAgent(),
                photo_clue_agent=PhotoClueAgent(describer=RecordedDescriber(recorded_descriptions(trace))),
            )
        else:
            agents = [a for a in default_event_agents() if a.name != "photo"]
            agents.append(RecordedPhotoAgent(recorded_photo_result(trace)))
            draft = await run_main_agent(
                request, event_agents=agents, question_agent=_SilentQuestionAgent()
            )
    elapsed = perf_counter() - started
    return {
        "version": version,
        "durationSec": round(elapsed, 1),
        "tokenUsage": usage.summary(),
        "events": [_event_summary(event, request) for event in draft.events],
        "result": build_result_request(draft, request).model_dump(by_alias=True, mode="json"),
        "warnings": [w.message for w in draft.warnings],
        "draft": draft.model_dump(by_alias=True, mode="json"),
    }


def _event_summary(event, request: TimelineDraftRequest) -> dict:
    return {
        "time": f"{_hhmm(event.start_time)}~{_hhmm(event.end_time)}",
        "type": event.event_type.value,
        "title": event.title,
        "description": event.description,
        "filled": filled_description(event),
        "place": event.place,
        "placeCandidates": event.place_candidates,
        "address": event.address,
        "sources": sorted({ref.source_type.value for ref in event.source_refs}),
        "placeNamesInTitle": place_names_in_title(event, request),
        "mergeReason": event.merge_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["skeleton", "run"])
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, help="run 결과를 저장할 디렉터리")
    args = parser.parse_args()

    for path in args.traces:
        trace = json.loads(path.read_text(encoding="utf-8"))
        name = path.stem.replace("trace-", "")[:8]
        if args.mode == "skeleton":
            print(f"# {name}\n{skeleton_report(trace)}\n")
            continue
        result = asyncio.run(replay(trace))
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            target = args.out / f"{name}-{result['version']}.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"# {name} {result['version']} {result['durationSec']}s {result['tokenUsage']}")
        for event in result["events"]:
            print(f"- {event['time']} {event['type']:13} {event['title']} | {event['filled']}")


if __name__ == "__main__":
    main()
