"""실제 LLM 테스트의 환경 검증과 콘솔 진행 로그."""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
SUPPORTED_PROVIDERS = ("openai", "gemini")


@dataclass(frozen=True)
class LiveLLMConfig:
    provider: str
    model: str
    serial: bool


def load_repo_env() -> None:
    """PyCharm 단독 실행에서도 저장소 루트의 ``.env``를 먼저 읽는다."""

    load_dotenv(ROOT_DIR / ".env", override=False)


def resolve_live_llm_config(environ: Mapping[str, str]) -> LiveLLMConfig:
    """Secret을 반환하지 않고 실제 LLM 실행 설정만 검증한다."""

    provider = environ.get("LLM_PROVIDER", "").lower()
    if not provider:
        provider = next(
            (
                candidate
                for candidate in SUPPORTED_PROVIDERS
                if environ.get(f"{candidate.upper()}_API_KEY")
            ),
            "",
        )
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            "LLM_PROVIDER는 openai 또는 gemini여야 합니다."
        )

    required = (f"{provider.upper()}_API_KEY", f"{provider.upper()}_MODEL")
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("실제 LLM 테스트 환경변수가 부족합니다: " + ", ".join(missing))

    return LiveLLMConfig(
        provider=provider,
        model=environ[required[1]],
        serial=environ.get("LAIMORY_LIVE_LLM_SERIAL") == "1",
    )


def prepare_live_llm_env() -> LiveLLMConfig:
    """opt-in 여부와 provider 설정을 확인하고 실행 설정을 반환한다."""

    load_repo_env()
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("LOG_LEVEL", "INFO")

    if os.getenv("LAIMORY_LIVE_LLM") != "1":
        pytest.skip("실제 LLM 테스트는 LAIMORY_LIVE_LLM=1일 때만 실행합니다.")

    try:
        config = resolve_live_llm_config(os.environ)
    except ValueError as exc:
        pytest.fail(str(exc))

    os.environ.setdefault("LLM_PROVIDER", config.provider)
    trace(
        "llm env: "
        f"provider={config.provider} model={config.model} serial={config.serial}"
    )
    return config


def trace(message: str) -> None:
    """pytest 캡처와 관계없이 live 테스트 진행 상황을 즉시 출력한다."""

    print(f"[live-llm] {message}", file=sys.__stdout__, flush=True)


@contextmanager
def trace_heartbeat(label: str, interval_seconds: float = 10.0):
    """오래 걸리는 LLM 호출 중 주기적으로 진행 중임을 알린다."""

    stop_event = threading.Event()
    started = time.perf_counter()

    def _run() -> None:
        while not stop_event.wait(interval_seconds):
            trace(f"{label} still running elapsed={time.perf_counter() - started:.1f}s")

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
    """live 실행 중에만 pytest 전역 출력 캡처를 잠시 중단한다."""

    load_repo_env()
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
