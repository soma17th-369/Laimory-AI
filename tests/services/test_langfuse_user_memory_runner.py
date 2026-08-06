"""User Memory 갱신 trace 가 Timeline 과 섞이지 않는지 검증 (#64).

두 작업은 지연·토큰·실패율을 따로 봐야 한다. 같은 trace 이름이나 tag 를 쓰면
대시보드에서 한 지표로 합쳐져 어느 쪽이 느려졌는지 알 수 없다.
"""

import asyncio
import json

from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.core import langfuse_tracing
from app.schemas import TaskStatus
from app.schemas.user_memory import UserMemory
from app.services import user_memory_runner
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.user_memory import daily_timeline, daily_timeline_event, update_request

_TASK_ID = "task-langfuse-user-memory"


class _StubAgent:
    def generate(self, existing, digest, *, violations=()):
        return UserMemory(basic_profile="30대 개발자입니다.")


def _exporter(monkeypatch, key: str):
    """테스트마다 다른 public key 를 쓴다.

    Langfuse SDK 는 public key 로 client 를 캐시한다. 같은 key 를 재사용하면 두
    번째 테스트가 첫 번째 테스트의 exporter 를 물어 span 이 하나도 안 보인다.
    """

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-user-memory-{key}",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:1",
        span_exporter=exporter,
    )
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(
        langfuse_tracing.settings, "langfuse_content_capture", "SANITIZED"
    )
    # flush 는 테스트가 직접 부른다. runner 가 부르면 to_thread 로 나가 순서가 흔들린다.
    monkeypatch.setattr(user_memory_runner.settings, "langfuse_enabled", False)
    return exporter, client


def _run(monkeypatch, key: str, **overrides):
    exporter, client = _exporter(monkeypatch, key)
    payload = update_request(taskId=_TASK_ID, **overrides)
    status = asyncio.run(
        user_memory_runner.process_user_memory_task(
            _TASK_ID,
            FakeAppServerClient(),
            payload,
            _StubAgent(),
        )
    )
    client.flush()
    return status, {span.name: span for span in exporter.get_finished_spans()}


def test_user_memory_task_opens_its_own_trace(monkeypatch) -> None:
    status, spans = _run(monkeypatch, "own-trace")

    assert status is TaskStatus.SUCCESS
    assert "update-user-memory" in spans
    # Timeline 의 이름은 하나도 쓰지 않는다.
    assert "generate-timeline" not in spans
    assert "finalize-timeline" not in spans


def test_trace_is_tagged_and_named_apart_from_timeline(monkeypatch) -> None:
    _, spans = _run(monkeypatch, "tagging")

    attributes = dict(spans["update-user-memory"].attributes)
    serialized = json.dumps(attributes, ensure_ascii=False)

    assert "user-memory" in serialized
    assert "timeline" not in serialized.replace("update-user-memory", "")


def test_root_output_carries_shape_not_content(monkeypatch) -> None:
    _, spans = _run(
        monkeypatch,
        "shape",
        dailyTimelines=[
            daily_timeline(events=[daily_timeline_event(memo="비밀 메모")])
        ],
    )

    output = json.loads(
        spans["update-user-memory"].attributes["langfuse.observation.output"]
    )

    assert output["status"] == TaskStatus.SUCCESS.value
    assert output["repairAttempts"] == 0
    # 본문이 아니라 모양만 남는다.
    assert output["userMemory"]["schemaVersion"] == "1.0"
    assert "30대 개발자입니다." not in json.dumps(output, ensure_ascii=False)


def test_no_secret_or_diary_body_reaches_the_trace(monkeypatch) -> None:
    _, spans = _run(
        monkeypatch,
        "no-leak",
        taskToken="must-never-appear",
        dailyTimelines=[
            daily_timeline(events=[daily_timeline_event(memo="비밀 메모")])
        ],
    )

    serialized = json.dumps(
        [dict(span.attributes) for span in spans.values()], ensure_ascii=False
    )

    assert "must-never-appear" not in serialized
    assert "비밀 메모" not in serialized
