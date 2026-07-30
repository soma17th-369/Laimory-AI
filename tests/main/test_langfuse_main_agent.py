"""실제 Langfuse SDK span으로 Main Agent 계층·입출력·토큰 집계를 검증한다."""

import asyncio
import json
from types import SimpleNamespace

from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.agents.main import run_main_agent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.core import langfuse_tracing
from app.core.llm import OpenAIProvider
from app.core.observability import (
    InMemoryObservationSink,
    Observer,
    observation_context,
)
from app.schemas import AgentEventResult
from tests.fixtures.fake_llm import candidate
from tests.fixtures.pipeline import StubEventAgent, confirm_only_repair_agent
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


class _FakeCompletions:
    def __init__(self, response) -> None:
        self.response = response

    def create(self, **kwargs):
        return self.response


def _provider(response_text: str) -> OpenAIProvider:
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=2),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=response_text),
            )
        ],
        usage=usage,
    )
    provider = object.__new__(OpenAIProvider)
    provider.model = "gpt-test"
    provider.api_key = "test"
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(response))
    )
    return provider


def _trace_output(span) -> dict:
    return json.loads(span.attributes["langfuse.observation.output"])


def test_main_agent_trace_has_full_recursive_hierarchy_and_rollups(
    monkeypatch,
) -> None:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-main-hierarchy",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:1",
        span_exporter=exporter,
    )
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(
        langfuse_tracing.settings,
        "langfuse_content_capture",
        "SANITIZED",
    )

    request = make_request(
        stays=[stay_item(1, raw_id="s-1")],
    )
    source_result = AgentEventResult.model_validate(
        {
            "candidates": [
                candidate("REST", [("STAY", "s-1")]),
            ]
        }
    )
    draft_json = json.dumps(
        {
            "events": [
                {
                    "eventType": "REST",
                    "title": "휴식",
                    "description": "체류",
                    "startTime": "2026-06-20T09:00:00+09:00",
                    "endTime": "2026-06-20T10:00:00+09:00",
                    "confidence": 0.8,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [
                        {
                            "sourceType": "STAY",
                            "rawId": fixture_raw_id("s-1"),
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
    observer = Observer(InMemoryObservationSink())

    async def run() -> None:
        with (
            observation_context(request.task_id, observer),
            langfuse_tracing.trace_timeline_task(
                request.task_id,
                daily_record_id=42,
                window_start=request.window.start,
                window_end=request.window.end,
            ),
        ):
            await run_main_agent(
                request,
                event_agents=[
                    StubEventAgent(source_result, name="source-a"),
                    StubEventAgent(AgentEventResult(), name="source-b"),
                ],
                timeline_agent=TimelineAgent(llm=_provider(draft_json)),
                repair_agent=confirm_only_repair_agent(),
            )

    asyncio.run(run())
    client.flush()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    expected = {
        "generate-timeline",
        "main-agent",
        "event-agent-source-a",
        "event-agent-source-b",
        "merge-event-results",
        "timeline-agent",
        "generate-timeline-draft",
        "repair-agent",
        "confirm-timeline-draft",
    }
    assert expected <= spans.keys()

    root = spans["generate-timeline"]
    main = spans["main-agent"]
    assert main.parent.span_id == root.context.span_id
    for child_name in (
        "event-agent-source-a",
        "event-agent-source-b",
        "merge-event-results",
        "timeline-agent",
        "repair-agent",
    ):
        assert spans[child_name].parent.span_id == main.context.span_id
    assert (
        spans["generate-timeline-draft"].parent.span_id
        == spans["timeline-agent"].context.span_id
    )
    assert (
        spans["confirm-timeline-draft"].parent.span_id
        == spans["repair-agent"].context.span_id
    )

    event_input = json.loads(
        spans["event-agent-source-a"].attributes[
            "langfuse.observation.input"
        ]
    )
    assert event_input["request"]["stays"][0]["rawId"] == fixture_raw_id("s-1")
    assert "result" in _trace_output(spans["event-agent-source-a"])
    assert "mergedResult" in _trace_output(spans["merge-event-results"])
    assert "timeline" in _trace_output(spans["timeline-agent"])
    assert "timeline" in _trace_output(spans["repair-agent"])

    generation = spans["generate-timeline-draft"]
    messages = json.loads(
        generation.attributes["langfuse.observation.input"]
    )
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "TimelineDraft" in messages[0]["content"]
    assert json.loads(
        generation.attributes["langfuse.observation.usage_details"]
    ) == {
        "input": 10,
        "input_cached_tokens": 2,
        "output": 5,
        "output_reasoning_tokens": 3,
    }

    main_output = _trace_output(main)
    assert main_output["durationMs"] >= 0
    assert main_output["tokenUsage"] == {
        "generationCount": 1,
        "inputTokens": 12,
        "outputTokens": 8,
        "totalTokens": 20,
        "byType": {
            "input": 10,
            "input_cached_tokens": 2,
            "output": 5,
            "output_reasoning_tokens": 3,
        },
    }
