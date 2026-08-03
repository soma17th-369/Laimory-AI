"""data/input 기반 Event Agent live 테스트 공통 헬퍼."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from app.agents.events.photo import PhotoEventAgent
from app.schemas import TimelineDraftRequest
from app.services.normalizer import normalize
from tests.fixtures.snapshot_file import load_snapshot_from_file

ROOT_DIR = Path(__file__).resolve().parents[2]
INPUT_DIR = ROOT_DIR / "data" / "input"
OUTPUT_DIR = ROOT_DIR / "data" / "output" / "event-agents"
SNAPSHOT_PATH = INPUT_DIR / "2026-07-08.json"


def vision_photo_event_agent() -> PhotoEventAgent:
    """기본 조립을 쓰는 Photo Event Agent.

    이슈 #52 이후 이미지 소스는 `photoUrl` 다운로드 하나다. `data/input` 스냅샷에는
    `photoUrl` 이 없으므로 사진 설명은 `describe_prompt.md` fallback 으로 채워지고,
    나머지 event 추론은 그대로 검증된다. 실제 이미지로 vision 경로를 보려면 스냅샷에
    유효한 `photoUrl` 을 넣기만 하면 된다 — 별도 설정은 없다(#59).
    """

    return PhotoEventAgent()


def load_repo_env() -> None:
    """repo root의 `.env`를 읽어 live LLM 실행 환경을 준비한다."""

    load_dotenv(ROOT_DIR / ".env", override=False)


def prepare_live_llm_env() -> None:
    load_repo_env()
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("LOG_LEVEL", "INFO")

    if os.getenv("LAIMORY_LIVE_LLM") != "1":
        pytest.skip("실제 LLM Event Agent 테스트는 LAIMORY_LIVE_LLM=1일 때만 실행합니다.")

    if not os.getenv("LLM_PROVIDER"):
        if os.getenv("OPENAI_API_KEY"):
            os.environ["LLM_PROVIDER"] = "openai"
        elif os.getenv("GEMINI_API_KEY"):
            os.environ["LLM_PROVIDER"] = "gemini"

    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider not in {"openai", "gemini"}:
        pytest.fail("LLM_PROVIDER=openai 또는 LLM_PROVIDER=gemini 설정이 필요합니다.")

    required = {
        "openai": ("OPENAI_API_KEY", "OPENAI_MODEL"),
        "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL"),
    }[provider]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.fail("실제 LLM 테스트 환경 변수가 부족합니다: " + ", ".join(missing))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_agent_result(agent_name: str, payload: dict[str, Any]) -> Path:
    output_path = OUTPUT_DIR / f"{agent_name}.live-actual.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def dump_agent_result(agent_name: str, result) -> dict[str, Any]:
    actual = result.model_dump(by_alias=True, mode="json")
    output_path = write_agent_result(agent_name, actual)
    print(
        f"\n[{agent_name}] output={output_path} "
        f"candidates={len(result.candidates)} "
        f"fragments={len(result.fragments)} "
        f"warnings={len(result.warnings)}"
    )
    print(json.dumps(actual, ensure_ascii=False, indent=2))
    return actual


def assert_agent_result_shape(actual: dict[str, Any], result) -> None:
    assert isinstance(actual["candidates"], list)
    assert isinstance(actual["fragments"], list)
    assert isinstance(actual["warnings"], list)
    assert result.candidates or result.fragments or result.warnings


def full_live_request() -> TimelineDraftRequest:
    """실제 입력 JSON 전체를 정규화한 요청을 만든다."""

    return normalize(load_snapshot_from_file(SNAPSHOT_PATH))


def _domain_request(
    request: TimelineDraftRequest, **domains: list
) -> TimelineDraftRequest:
    return request.model_copy(
        update={
            "stays": domains.get("stays", []),
            "movements": domains.get("movements", []),
            "calendars": domains.get("calendars", []),
            "healths": domains.get("healths", []),
            "notifications": domains.get("notifications", []),
            "photos": domains.get("photos", []),
        }
    )


def stay_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(
        request,
        stays=request.stays,
        movements=request.movements,
    )


def calendar_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, calendars=request.calendars)


def photo_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, photos=request.photos)


def notification_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, notifications=request.notifications)


def health_request() -> TimelineDraftRequest:
    request = full_live_request()
    return _domain_request(request, healths=request.healths)
