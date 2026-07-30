"""Timeline runner 최상위 trace의 조회·정규화·저장·콜백·FINAL 입출력 검증."""

import asyncio
import json

from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.core import langfuse_tracing
from app.schemas import TaskStatus, TimelineDraft
from app.services import timeline_runner
from app.services.source_repository import InMemorySourceRepository
from app.services.timeline_repository import NoopTimelineRepository
from tests.fixtures.requests import default_source_items, make_snapshot

_TASK_ID = "task-langfuse-runner"
_WINDOW_START = "2026-06-20T00:00:00+09:00"
_WINDOW_END = "2026-06-21T00:00:00+09:00"


def _json_attribute(span, name: str):
    return json.loads(span.attributes[name])


def test_runner_trace_contains_every_boundary_input_and_output(
    monkeypatch,
) -> None:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-runner-hierarchy",
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
    monkeypatch.setattr(timeline_runner.settings, "langfuse_enabled", False)
    monkeypatch.setattr(timeline_runner.settings, "obs_enabled", False)
    monkeypatch.setattr(timeline_runner.settings, "es_url", "")
    monkeypatch.setattr(timeline_runner.settings, "obs_local_dir", None)
    monkeypatch.setattr(
        timeline_runner.settings,
        "app_server_api_url",
        "https://app.example/s/api/v1",
    )

    draft = TimelineDraft(
        user_id="u-1",
        date="2026-06-20",
        timezone="Asia/Seoul",
    )

    async def fake_main_agent(request):
        return draft

    async def fake_send_callback(
        app_server_api_url,
        task_id,
        callback_token,
        payload,
    ):
        return True

    monkeypatch.setattr(timeline_runner, "run_main_agent", fake_main_agent)
    monkeypatch.setattr(
        timeline_runner,
        "send_callback",
        fake_send_callback,
    )

    repo = InMemorySourceRepository()
    repo.put(
        make_snapshot(
            task_id=_TASK_ID,
            source_items=default_source_items(),
        )
    )

    status = asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID,
            repo,
            NoopTimelineRepository(),
            42,
            _WINDOW_START,
            _WINDOW_END,
            "must-never-appear",
        )
    )
    client.flush()

    assert status is TaskStatus.SUCCESS
    spans = {span.name: span for span in exporter.get_finished_spans()}
    expected = {
        "generate-timeline",
        "retrieve-source-snapshot",
        "normalize-source-snapshot",
        "store-timeline",
        "send-completion-callback",
        "finalize-timeline",
    }
    assert expected <= spans.keys()

    root = spans["generate-timeline"]
    for child_name in expected - {"generate-timeline"}:
        assert spans[child_name].parent.span_id == root.context.span_id

    retrieval_output = _json_attribute(
        spans["retrieve-source-snapshot"],
        "langfuse.observation.output",
    )
    assert retrieval_output["snapshot"]["taskId"] == _TASK_ID

    normalized_output = _json_attribute(
        spans["normalize-source-snapshot"],
        "langfuse.observation.output",
    )
    assert normalized_output["request"]["taskId"] == _TASK_ID

    storage_input = _json_attribute(
        spans["store-timeline"],
        "langfuse.observation.input",
    )
    assert storage_input["timeline"]["date"] == "2026-06-20"

    callback_input = _json_attribute(
        spans["send-completion-callback"],
        "langfuse.observation.input",
    )
    assert callback_input["payload"]["status"] == "SUCCESS"

    root_input = _json_attribute(root, "langfuse.observation.input")
    root_output = _json_attribute(root, "langfuse.observation.output")
    assert root_input["request"]["taskId"] == _TASK_ID
    assert root_output["status"] == "SUCCESS"
    assert root_output["timeline"]["date"] == "2026-06-20"
    assert root_output["durationMs"] >= 0
    assert root_output["tokenUsage"]["generationCount"] == 0

    serialized_attributes = json.dumps(
        [dict(span.attributes) for span in spans.values()],
        ensure_ascii=False,
    )
    assert "must-never-appear" not in serialized_attributes
