"""Langfuse 어댑터의 no-op·콘텐츠 보호·trace 계약을 검증한다."""

import asyncio
from contextlib import contextmanager

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import SecretStr

from app.core import langfuse_tracing
from app.core.error_codes import ErrorCode
from app.core.llm import OpenAIProvider


class _FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self


class _FakeLangfuse:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.observations: list[_FakeObservation] = []
        self.flush_count = 0

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.started.append(kwargs)
        observation = _FakeObservation()
        self.observations.append(observation)
        yield observation

    def create_trace_id(self, *, seed: str) -> str:
        return ("0" * 32) if seed else ("f" * 32)

    def flush(self) -> None:
        self.flush_count += 1


class _FailingContextManager:
    def __init__(self, *, fail_enter: bool = False, fail_exit: bool = False) -> None:
        self.fail_enter = fail_enter
        self.fail_exit = fail_exit
        self.observation = _FakeObservation()

    def __enter__(self):
        if self.fail_enter:
            raise RuntimeError("tracing enter failed")
        return self.observation

    def __exit__(self, exc_type, exc, traceback):
        if self.fail_exit:
            raise RuntimeError("tracing exit failed")
        return False


class _FailingLangfuse(_FakeLangfuse):
    def __init__(self, *, fail_enter: bool = False, fail_exit: bool = False) -> None:
        super().__init__()
        self.fail_enter = fail_enter
        self.fail_exit = fail_exit

    def start_as_current_observation(self, **kwargs):
        self.started.append(kwargs)
        return _FailingContextManager(
            fail_enter=self.fail_enter,
            fail_exit=self.fail_exit,
        )


def test_none_capture_never_exposes_original_content(monkeypatch) -> None:
    monkeypatch.setattr(
        langfuse_tracing.settings, "langfuse_content_capture", "NONE"
    )

    captured = langfuse_tracing.capture_langfuse_content(
        {"prompt": "서울 집에서 user@example.com에게 연락"}, field="input"
    )

    assert captured["contentCaptured"] is False
    assert captured["byteLength"] > 0
    assert len(captured["sha256"]) == 64
    assert "서울" not in str(captured)
    assert "user@example.com" not in str(captured)


def test_sanitized_capture_masks_secrets_and_contact_data(monkeypatch) -> None:
    monkeypatch.setattr(
        langfuse_tracing.settings, "langfuse_content_capture", "SANITIZED"
    )

    captured = langfuse_tracing.capture_langfuse_content(
        {
            "authorization": "Bearer token-value",
            "email": "user@example.com",
            "phone": "010-1234-5678",
        },
        field="input",
    )

    assert captured["authorization"] == "[REDACTED]"
    assert captured["email"] == "[REDACTED]"
    assert captured["phone"] == "[REDACTED]"


def test_trace_observation_records_stable_type_model_and_usage(monkeypatch) -> None:
    fake = _FakeLangfuse()
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)
    monkeypatch.setattr(
        langfuse_tracing.settings, "langfuse_content_capture", "SANITIZED"
    )

    with langfuse_tracing.trace_observation(
        "call-llm",
        as_type="generation",
        input=[{"role": "user", "content": "안녕"}],
        model="test-model",
        model_parameters={"temperature": 0.2},
        metadata={"provider": "fake"},
    ) as observation:
        langfuse_tracing.update_observation(
            observation,
            output=[{"role": "assistant", "content": "반가워요"}],
            usage_details={"input": 3, "output": 4, "total": 7},
        )

    assert fake.started[0]["name"] == "call-llm"
    assert fake.started[0]["as_type"] == "generation"
    assert fake.started[0]["model"] == "test-model"
    assert fake.started[0]["input"][0]["role"] == "user"
    assert fake.observations[0].updates[0]["usage_details"] == {
        "input": 3,
        "output": 4,
        "total": 7,
    }


def test_timeline_trace_uses_task_id_based_trace_context(monkeypatch) -> None:
    fake = _FakeLangfuse()
    propagated: dict = {}

    @contextmanager
    def fake_propagate_attributes(**kwargs):
        propagated.update(kwargs)
        yield

    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)
    monkeypatch.setattr(
        langfuse_tracing,
        "propagate_attributes",
        fake_propagate_attributes,
    )
    monkeypatch.setattr(
        langfuse_tracing.settings, "langfuse_content_capture", "SANITIZED"
    )

    with langfuse_tracing.trace_timeline_task(
        "task-123",
        daily_record_id=42,
        window_start="2026-07-30T00:00:00+09:00",
        window_end="2026-07-31T00:00:00+09:00",
    ):
        pass

    root = fake.started[0]
    assert root["name"] == "generate-timeline"
    assert root["as_type"] == "span"
    assert root["trace_context"] == {"trace_id": "0" * 32}
    assert propagated["session_id"] == "task-123"
    assert propagated["metadata"]["taskId"] == "task-123"


def test_enabled_without_credentials_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_tracing.settings, "langfuse_enabled", True)
    monkeypatch.setattr(langfuse_tracing.settings, "langfuse_public_key", "")
    monkeypatch.setattr(
        langfuse_tracing.settings, "langfuse_secret_key", SecretStr("")
    )
    langfuse_tracing.get_langfuse_client.cache_clear()

    assert langfuse_tracing.get_langfuse_client() is None

    langfuse_tracing.get_langfuse_client.cache_clear()


def test_observation_enter_failure_is_noop(monkeypatch) -> None:
    fake = _FailingLangfuse(fail_enter=True)
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)

    executed = False
    with langfuse_tracing.trace_observation("broken-start") as observation:
        executed = True
        assert observation is None

    assert executed is True


def test_observation_exit_failure_does_not_fail_business_logic(monkeypatch) -> None:
    fake = _FailingLangfuse(fail_exit=True)
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)

    with langfuse_tracing.trace_observation("broken-end") as observation:
        assert observation is not None


def test_observation_exit_failure_preserves_business_exception(monkeypatch) -> None:
    fake = _FailingLangfuse(fail_exit=True)
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)

    with pytest.raises(ValueError, match="business failed"):
        with langfuse_tracing.trace_observation("business-error"):
            raise ValueError("business failed")


def test_update_capture_failure_is_isolated(monkeypatch) -> None:
    observation = _FakeObservation()
    monkeypatch.setattr(
        langfuse_tracing,
        "capture_langfuse_content",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )

    langfuse_tracing.update_observation(observation, output={"value": "ok"})

    assert observation.updates == []


def test_otel_parent_context_propagates_through_asyncio_to_thread(
    monkeypatch,
) -> None:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-test",
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

    def create_child() -> None:
        with langfuse_tracing.trace_observation("thread-child"):
            pass

    async def run() -> None:
        with langfuse_tracing.trace_observation("async-parent"):
            await asyncio.to_thread(create_child)

    asyncio.run(run())
    client.flush()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["thread-child"].parent is not None
    assert (
        spans["thread-child"].parent.span_id
        == spans["async-parent"].context.span_id
    )


def test_nested_token_usage_scopes_roll_up_into_parent() -> None:
    with langfuse_tracing.token_usage_scope() as parent:
        langfuse_tracing.record_token_usage({"input": 3, "output": 2})
        with langfuse_tracing.token_usage_scope() as child:
            langfuse_tracing.record_token_usage(
                {
                    "input": 1,
                    "input_cached_tokens": 4,
                    "output_reasoning_tokens": 2,
                }
            )

    assert child.summary() == {
        "generationCount": 1,
        "inputTokens": 5,
        "outputTokens": 2,
        "totalTokens": 7,
        "byType": {
            "input": 1,
            "input_cached_tokens": 4,
            "output_reasoning_tokens": 2,
        },
    }
    assert parent.summary() == {
        "generationCount": 2,
        "inputTokens": 8,
        "outputTokens": 4,
        "totalTokens": 12,
        "byType": {
            "input": 4,
            "input_cached_tokens": 4,
            "output": 2,
            "output_reasoning_tokens": 2,
        },
    }


def test_generation_keeps_prompts_options_and_vision_metadata(
    monkeypatch,
) -> None:
    fake = _FakeLangfuse()
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)
    monkeypatch.setattr(
        langfuse_tracing.settings,
        "langfuse_content_capture",
        "SANITIZED",
    )
    provider = object.__new__(OpenAIProvider)
    provider.model = "gpt-test"

    with provider._trace_generation(
        "사진 설명",
        system="시스템 프롬프트",
        temperature=0.2,
        image_count=2,
        image_mime_types=["image/png", "image/jpeg"],
        options={"max_tokens": 100, "response_format": {"type": "json_object"}},
    ):
        pass

    started = fake.started[0]
    assert started["name"] == "describe-photo-images"
    assert started["input"] == [
        {"role": "system", "content": "시스템 프롬프트"},
        {"role": "user", "content": "사진 설명"},
    ]
    assert started["metadata"]["imageCount"] == 2
    assert started["metadata"]["imageMimeTypes"] == [
        "image/jpeg",
        "image/png",
    ]
    assert started["metadata"]["options"]["max_tokens"] == 100


def test_generation_failure_records_safe_error_code(monkeypatch) -> None:
    fake = _FakeLangfuse()
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: fake)
    monkeypatch.setattr(
        langfuse_tracing.settings,
        "langfuse_content_capture",
        "SANITIZED",
    )
    provider = object.__new__(OpenAIProvider)
    provider.model = "gpt-test"

    with pytest.raises(RuntimeError, match="provider secret failure"):
        with provider._trace_generation(
            "프롬프트",
            system=None,
            temperature=0.2,
        ):
            raise RuntimeError("provider secret failure")

    update = fake.observations[0].updates[0]
    assert update["output"] == {
        "errorCode": int(ErrorCode.LLM_CALL_FAILED),
        "errorType": "RuntimeError",
    }
    assert "provider secret failure" not in str(update)
