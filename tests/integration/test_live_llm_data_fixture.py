"""실제 LLM으로 날짜별 fixture를 실행하는 opt-in 전체 파이프라인 테스트.

기본 테스트 실행에서는 네트워크와 비용이 발생하지 않도록 건너뛴다. 실행하려면
``LAIMORY_LIVE_LLM=1``과 provider별 API key/model 설정이 필요하다.
"""

import asyncio
import difflib
import json
import os
from pathlib import Path

import pytest

from tests.fixtures.live_llm import (
    live_trace_console,
    prepare_live_llm_env,
    trace,
)
from tests.fixtures.live_pipeline import TracedEventAgent, TracedTimelineAgent

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
EXPECTED_PATH = DATA_DIR / "output" / "timeline-draft.expected.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_for_compare(payload: dict) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return [*text.splitlines(), ""]


@pytest.mark.integration
@pytest.mark.live_llm
def test_live_llm_pipeline_matches_data_fixture_shape_and_writes_comparison(
    live_trace_console,
):
    prepare_live_llm_env()

    from app.agents.events import default_event_agents
    from app.agents.events.photo import PhotoEventAgent
    from app.agents.events.photo.describer import VisionPhotoDescriber
    from app.agents.events.photo.image_source import LocalFilePhotoImageSource
    from app.agents.main import run_main_agent
    from app.agents.timeline.timeline_agent import TimelineAgent
    from app.services.normalizer import normalize
    from tests.fixtures.live_data import resolve_live_data_case
    from tests.fixtures.live_output import current_live_run
    from tests.agents.live_input_helpers import live_observer

    trace("load input snapshot")
    live_data = resolve_live_data_case()
    live_run = current_live_run(live_data.date)
    expected = _load_json(EXPECTED_PATH)
    request = normalize(live_data.load_snapshot())
    trace(
        "request ready: "
        f"date={request.date} "
        f"source_items={len(request.iter_source_items())} "
        f"notifications={len(request.notifications)} "
        f"photos={len(request.photos)}"
    )

    # Photo Agent만 날짜별 fixture의 실제 사진 파일을 읽는 vision 구성으로 교체한다.
    photo_image_source = LocalFilePhotoImageSource(live_data.image_dir)
    raw_agents = [
        PhotoEventAgent(
            describer=VisionPhotoDescriber(image_source=photo_image_source)
        )
        if getattr(agent, "name", None) == "photo"
        else agent
        for agent in default_event_agents()
    ]
    event_agents = [TracedEventAgent(agent) for agent in raw_agents]
    timeline_agent = TracedTimelineAgent(TimelineAgent())

    trace("main agent start")
    observer = live_observer(request.date)
    draft = asyncio.run(
        run_main_agent(
            request,
            event_agents=event_agents,
            timeline_agent=timeline_agent,
            observer=observer,
        )
    )
    observer.assert_llm_calls_succeeded()
    trace("main agent done")

    actual = draft.model_dump(by_alias=True, mode="json")
    actual_path = live_run.write_json("timeline-draft.actual.json", actual)
    trace(f"write actual: {actual_path}")

    trace("compare expected vs actual")
    diff = list(
        difflib.unified_diff(
            _dump_for_compare(expected),
            _dump_for_compare(actual),
            fromfile=str(EXPECTED_PATH),
            tofile=str(actual_path),
            lineterm="",
        )
    )
    diff_path = live_run.write_text(
        "timeline-draft.diff.txt",
        "\n".join(diff) + ("\n" if diff else ""),
    )

    print(f"\nactual: {actual_path}")
    print(f"diff: {diff_path}")
    print(
        "summary: "
        f"expected events={len(expected.get('events', []))}, "
        f"actual events={len(actual.get('events', []))}, "
        f"questions={len(actual.get('questions', []))}, "
        f"warnings={len(actual.get('warnings', []))}"
    )
    print("\nactual result:")
    print(json.dumps(actual, ensure_ascii=False, indent=2))

    assert actual["date"] == request.date
    assert actual["timezone"] == request.timezone
    assert isinstance(actual["events"], list)
    assert isinstance(actual["questions"], list)
    assert isinstance(actual["warnings"], list)

    if os.getenv("LAIMORY_LIVE_LLM_STRICT") == "1":
        assert actual == expected
