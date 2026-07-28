"""실제 LLM 으로 data fixture 를 실행하는 opt-in 통합 테스트.

기본 테스트 실행에서는 네트워크/비용이 발생하지 않도록 skip 한다.
실행하려면 `LAIMORY_LIVE_LLM=1` 과 provider 별 API key/model 설정이 필요하다.
"""

import asyncio
import difflib
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "input" / "2026-07-08.json"
EXPECTED_PATH = DATA_DIR / "output" / "timeline-draft.expected.json"
ACTUAL_PATH = DATA_DIR / "output" / "timeline-draft.live-actual.json"
DIFF_PATH = DATA_DIR / "output" / "timeline-draft.live-diff.txt"


def _load_repo_env() -> None:
    """PyCharm 단독 실행에서도 repo root 의 `.env` 를 먼저 읽는다."""

    load_dotenv(ROOT_DIR / ".env", override=False)


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _dump_for_compare(payload: dict) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return [*text.splitlines(), ""]


def _prepare_live_llm_env() -> None:
    """앱 import 전에 live LLM 테스트에 필요한 최소 환경을 확인한다."""

    _load_repo_env()

    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("LOG_LEVEL", "INFO")

    if not os.getenv("LLM_PROVIDER"):
        if os.getenv("OPENAI_API_KEY"):
            os.environ["LLM_PROVIDER"] = "openai"
        elif os.getenv("GEMINI_API_KEY"):
            os.environ["LLM_PROVIDER"] = "gemini"

    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider not in {"openai", "gemini"}:
        pytest.fail(
            "실제 LLM 테스트에는 LLM_PROVIDER=openai 또는 LLM_PROVIDER=gemini 가 필요합니다."
        )

    required = {
        "openai": ("OPENAI_API_KEY", "OPENAI_MODEL"),
        "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL"),
    }[provider]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.fail(
            "실제 LLM 테스트 환경 변수가 부족합니다: " + ", ".join(missing)
        )

    _trace(
        "llm env: "
        f"provider={provider} "
        f"model={os.getenv(required[1])} "
        f"serial={os.getenv('LAIMORY_LIVE_LLM_SERIAL') == '1'}"
    )


def _trace(message: str) -> None:
    """PyCharm pytest 콘솔에서도 바로 보이도록 live test 진행 상황을 출력한다."""

    print(f"[live-llm] {message}", file=sys.__stdout__, flush=True)


_SERIAL_AGENT_LOCK = threading.Lock()


@contextmanager
def _trace_heartbeat(label: str, interval_seconds: float = 10.0):
    """LLM 호출이 길어질 때 콘솔에 주기적으로 진행 중 로그를 남긴다."""

    stop_event = threading.Event()
    started = time.perf_counter()

    def _run() -> None:
        while not stop_event.wait(interval_seconds):
            elapsed = time.perf_counter() - started
            _trace(f"{label} still running elapsed={elapsed:.1f}s")

    thread = threading.Thread(
        target=_run,
        name=f"live-llm-heartbeat-{label}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1.0)


@pytest.fixture
def live_trace_console(request):
    """live LLM 테스트 중 pytest 출력 캡처를 잠시 꺼서 진행 상황을 바로 보여준다."""

    _load_repo_env()
    if os.getenv("LAIMORY_LIVE_LLM") != "1":
        yield
        return

    capture_manager = request.config.pluginmanager.getplugin("capturemanager")
    if capture_manager is None:
        yield
        return

    capture_manager.suspend_global_capture(in_=True)
    try:
        yield
    finally:
        capture_manager.resume_global_capture()


class _TracedEventAgent:
    """live test 에서만 Event Agent 실행 전후를 콘솔에 남기는 wrapper."""

    def __init__(self, agent) -> None:
        self._agent = agent
        self.name = getattr(agent, "name", agent.__class__.__name__)

    def generate(self, request):
        _trace(f"event agent start: {self.name}")
        started = time.perf_counter()
        if os.getenv("LAIMORY_LIVE_LLM_SERIAL") == "1":
            with _SERIAL_AGENT_LOCK:
                _trace(f"event agent acquired serial slot: {self.name}")
                with _trace_heartbeat(f"event agent {self.name}"):
                    result = self._agent.generate(request)
        else:
            with _trace_heartbeat(f"event agent {self.name}"):
                result = self._agent.generate(request)
        elapsed = time.perf_counter() - started
        _trace(
            f"event agent done: {self.name} "
            f"candidates={len(result.candidates)} "
            f"fragments={len(result.fragments)} "
            f"warnings={len(result.warnings)} "
            f"elapsed={elapsed:.1f}s"
        )
        for warning in result.warnings:
            _trace(f"event agent warning: {self.name}: {warning.message}")
        return result


class _TracedTimelineAgent:
    """live test 에서만 Timeline Agent 실행 전후를 콘솔에 남기는 wrapper."""

    name = "timeline"

    def __init__(self, agent) -> None:
        self._agent = agent

    def generate(self, request, agent_result):
        _trace(
            "timeline agent start: "
            f"candidates={len(agent_result.candidates)} "
            f"fragments={len(agent_result.fragments)} "
            f"warnings={len(agent_result.warnings)}"
        )
        started = time.perf_counter()
        with _trace_heartbeat("timeline agent"):
            draft = self._agent.generate(request, agent_result)
        elapsed = time.perf_counter() - started
        _trace(
            "timeline agent done: "
            f"events={len(draft.events)} "
            f"questions={len(draft.questions)} "
            f"warnings={len(draft.warnings)} "
            f"elapsed={elapsed:.1f}s"
        )
        return draft


@pytest.mark.integration
@pytest.mark.live_llm
def test_live_llm_pipeline_matches_data_fixture_shape_and_writes_comparison(
    live_trace_console,
):
    _load_repo_env()

    if os.getenv("LAIMORY_LIVE_LLM") != "1":
        pytest.skip("실제 LLM 호출은 LAIMORY_LIVE_LLM=1 일 때만 실행합니다.")

    _prepare_live_llm_env()

    from app.agents.events import default_event_agents
    from app.agents.events.photo import PhotoEventAgent
    from app.agents.events.photo.describer import VisionPhotoDescriber
    from app.agents.events.photo.image_source import LocalFilePhotoImageSource
    from app.agents.timeline.timeline_agent import TimelineAgent
    from app.agents.main import run_main_agent
    from app.services.normalizer import normalize
    from app.services.source_repository import load_snapshot_from_file

    _trace("load input snapshot")
    expected = _load_json(EXPECTED_PATH)
    snapshot = load_snapshot_from_file(SNAPSHOT_PATH)
    request = normalize(snapshot)
    source_count = len(request.iter_source_items())
    _trace(
        "request ready: "
        f"date={request.date} "
        f"source_items={source_count} "
        f"notifications={len(request.notifications)} "
        f"photos={len(request.photos)}"
    )

    # 기본 파이프라인의 Photo Agent 를 data/input 실제 이미지를 vision 으로 보는
    # 것으로 교체한다(입력 JSON 의 photoFile=clientPhotoUri 가 실제 파일명과 일치).
    photo_image_source = LocalFilePhotoImageSource(DATA_DIR / "input")
    raw_agents = [
        PhotoEventAgent(
            describer=VisionPhotoDescriber(image_source=photo_image_source)
        )
        if getattr(agent, "name", None) == "photo"
        else agent
        for agent in default_event_agents()
    ]
    event_agents = [_TracedEventAgent(agent) for agent in raw_agents]
    timeline_agent = _TracedTimelineAgent(TimelineAgent())

    _trace("main agent start")
    draft = asyncio.run(
        run_main_agent(
            request,
            event_agents=event_agents,
            timeline_agent=timeline_agent,
        )
    )
    _trace("main agent done")

    actual = draft.model_dump(by_alias=True, mode="json")
    _trace(f"write actual: {ACTUAL_PATH}")
    _write_json(ACTUAL_PATH, actual)

    _trace("compare expected vs actual")
    diff = list(
        difflib.unified_diff(
            _dump_for_compare(expected),
            _dump_for_compare(actual),
            fromfile=str(EXPECTED_PATH),
            tofile=str(ACTUAL_PATH),
            lineterm="",
        )
    )
    DIFF_PATH.write_text("\n".join(diff) + ("\n" if diff else ""), encoding="utf-8")

    print(f"\nactual: {ACTUAL_PATH}")
    print(f"diff: {DIFF_PATH}")
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
