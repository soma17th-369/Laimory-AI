"""실제 LLM 전체 파이프라인의 진행 상황을 보여주는 테스트 wrapper."""

from __future__ import annotations

import os
import threading
import time

from tests.fixtures.live_llm import trace, trace_heartbeat

_SERIAL_AGENT_LOCK = threading.Lock()


class TracedEventAgent:
    """Event Agent 실행 전후와 결과 개수를 콘솔에 기록한다."""

    def __init__(self, agent) -> None:
        self._agent = agent
        self.name = getattr(agent, "name", agent.__class__.__name__)

    def generate(self, request):
        trace(f"event agent start: {self.name}")
        started = time.perf_counter()
        if os.getenv("LAIMORY_LIVE_LLM_SERIAL") == "1":
            with _SERIAL_AGENT_LOCK:
                trace(f"event agent acquired serial slot: {self.name}")
                with trace_heartbeat(f"event agent {self.name}"):
                    result = self._agent.generate(request)
        else:
            with trace_heartbeat(f"event agent {self.name}"):
                result = self._agent.generate(request)

        trace(
            f"event agent done: {self.name} "
            f"candidates={len(result.candidates)} "
            f"fragments={len(result.fragments)} "
            f"warnings={len(result.warnings)} "
            f"elapsed={time.perf_counter() - started:.1f}s"
        )
        for warning in result.warnings:
            trace(f"event agent warning: {self.name}: {warning.message}")
        return result


class TracedTimelineAgent:
    """Timeline Agent 실행 전후와 draft 개수를 콘솔에 기록한다."""

    name = "timeline"

    def __init__(self, agent) -> None:
        self._agent = agent

    def generate(self, request, agent_result):
        trace(
            "timeline agent start: "
            f"candidates={len(agent_result.candidates)} "
            f"fragments={len(agent_result.fragments)} "
            f"warnings={len(agent_result.warnings)}"
        )
        started = time.perf_counter()
        with trace_heartbeat("timeline agent"):
            draft = self._agent.generate(request, agent_result)
        trace(
            "timeline agent done: "
            f"events={len(draft.events)} "
            f"questions={len(draft.questions)} "
            f"warnings={len(draft.warnings)} "
            f"elapsed={time.perf_counter() - started:.1f}s"
        )
        return draft
