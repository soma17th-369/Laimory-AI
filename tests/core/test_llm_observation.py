"""공통 LLM 호출 경계의 프롬프트·응답 관측 검증."""

from types import SimpleNamespace

import pytest

from app.core.llm import (
    ImageInput,
    GeminiProvider,
    LLMClient,
    LLMCompletion,
    OpenAIProvider,
    TokenUsage,
    _gemini_usage,
    _openai_usage,
)
from app.core.observability import (
    ContentCapture,
    InMemoryObservationSink,
    ObservationEventType,
    ObservationStage,
    Observer,
    observation_context,
    observation_scope,
)


class _FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        response: str | LLMCompletion | Exception = "응답",
    ) -> None:
        self.response = response

    def _complete(self) -> str | LLMCompletion:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def complete(self, prompt: str, **kwargs) -> str:
        return self._complete()

    def complete_with_images(self, prompt: str, images, **kwargs) -> str:
        return self._complete()


def _client(provider: _FakeProvider) -> LLMClient:
    client = object.__new__(LLMClient)
    client.provider = provider
    return client


def test_llm_client_records_prompt_response_and_inherited_agent() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)

    with observation_context("tx-llm", observer):
        with observation_scope(ObservationStage.EVENT_AGENT, agent="location"):
            result = _client(_FakeProvider()).complete(
                "user@example.com의 위치를 분석",
                system="system prompt",
                temperature=0.2,
            )

    assert result == "응답"
    assert [event.event_type for event in sink.events] == [
        ObservationEventType.PROMPT,
        ObservationEventType.RESPONSE,
    ]
    assert {event.stage for event in sink.events} == {ObservationStage.LLM}
    assert {event.agent for event in sink.events} == {"location"}
    assert {event.transaction_id for event in sink.events} == {"tx-llm"}
    assert sink.events[0].payload["prompt"].startswith("[REDACTED]")
    assert sink.events[1].payload == {"response": "응답"}
    assert sink.events[1].duration_ms is not None
    assert sink.events[1].input_tokens is None


def test_llm_client_records_failure_and_reraises() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)

    with observation_context("tx-llm", observer):
        with pytest.raises(RuntimeError, match="provider down"):
            _client(_FakeProvider(RuntimeError("provider down"))).complete("prompt")

    assert sink.events[-1].event_type is ObservationEventType.FAILED
    assert sink.events[-1].payload["errorType"] == "RuntimeError"


def test_llm_vision_observation_records_metadata_not_image_bytes() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)
    image = ImageInput(data=b"sensitive-image", mime_type="image/jpeg")

    with observation_context("tx-vision", observer):
        _client(_FakeProvider()).complete_with_images("사진 분석", [image])

    prompt = sink.events[0]
    assert prompt.payload["images"] == [
        {"mimeType": "image/jpeg", "byteLength": len(image.data)}
    ]
    assert "sensitive-image" not in str(prompt.payload)


def test_llm_client_records_provider_reported_usage() -> None:
    sink = InMemoryObservationSink()
    observer = Observer(sink, content_capture=ContentCapture.SANITIZED)
    completion = LLMCompletion(
        text="응답",
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=25,
            total_tokens=125,
            cached_tokens=60,
            reasoning_tokens=5,
            tool_tokens=2,
            provider_details={"source": "provider"},
        ),
    )

    with observation_context("tx-usage", observer):
        result = _client(_FakeProvider(completion)).complete("prompt")

    response = sink.events[-1]
    assert result == "응답"
    assert response.input_tokens == 100
    assert response.output_tokens == 25
    assert response.total_tokens == 125
    assert response.cached_tokens == 60
    assert response.reasoning_tokens == 5
    assert response.tool_tokens == 2
    assert response.payload["usageDetails"] == {"source": "provider"}


class _UsageModel(SimpleNamespace):
    def model_dump(self, **kwargs):
        aliases = {
            "cached_tokens": "cachedTokens",
            "reasoning_tokens": "reasoningTokens",
        }
        return {
            aliases.get(key, key): value
            for key, value in vars(self).items()
            if value is not None
        }


def test_openai_usage_mapping_preserves_server_counts_and_details() -> None:
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        prompt_tokens_details=_UsageModel(cached_tokens=80),
        completion_tokens_details=_UsageModel(reasoning_tokens=10),
    )

    mapped = _openai_usage(SimpleNamespace(usage=usage))

    assert mapped == TokenUsage(
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        cached_tokens=80,
        reasoning_tokens=10,
        provider_details={
            "promptTokensDetails": {"cachedTokens": 80},
            "completionTokensDetails": {"reasoningTokens": 10},
        },
    )


def test_gemini_usage_mapping_keeps_thought_and_tool_counts_separate() -> None:
    metadata = _UsageModel(
        prompt_token_count=200,
        candidates_token_count=40,
        total_token_count=260,
        cached_content_token_count=100,
        thoughts_token_count=15,
        tool_use_prompt_token_count=5,
    )

    mapped = _gemini_usage(SimpleNamespace(usage_metadata=metadata))

    assert mapped.input_tokens == 200
    assert mapped.output_tokens == 40
    assert mapped.total_tokens == 260
    assert mapped.cached_tokens == 100
    assert mapped.reasoning_tokens == 15
    assert mapped.tool_tokens == 5


class _ResponseFactory:
    def __init__(self, response) -> None:
        self.response = response

    def create(self, **kwargs):
        return self.response

    def generate_content(self, **kwargs):
        return self.response


def test_openai_provider_returns_text_and_usage_from_same_response() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="openai 응답"))],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )
    provider = object.__new__(OpenAIProvider)
    provider.model = "openai-model"
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_ResponseFactory(response))
    )

    completion = provider.complete("prompt")

    assert completion.text == "openai 응답"
    assert completion.usage.total_tokens == 14


def test_gemini_provider_returns_text_and_usage_from_same_response() -> None:
    response = SimpleNamespace(
        text="gemini 응답",
        usage_metadata=_UsageModel(
            prompt_token_count=11,
            candidates_token_count=5,
            total_token_count=16,
            cached_content_token_count=None,
            thoughts_token_count=None,
            tool_use_prompt_token_count=None,
        ),
    )
    provider = object.__new__(GeminiProvider)
    provider.model = "gemini-model"
    provider.client = SimpleNamespace(models=_ResponseFactory(response))

    completion = provider.complete("prompt")

    assert completion.text == "gemini 응답"
    assert completion.usage.total_tokens == 16


def test_missing_provider_usage_is_not_reported_as_zero() -> None:
    assert _openai_usage(SimpleNamespace(usage=None)) is None
    assert _gemini_usage(SimpleNamespace(usage_metadata=None)) is None


class _FailingSink:
    def write(self, event) -> None:
        raise RuntimeError("sink down")


def test_observation_sink_failure_does_not_change_llm_result() -> None:
    observer = Observer(
        _FailingSink(),
        content_capture=ContentCapture.SANITIZED,
    )

    with observation_context("tx-sink-failure", observer):
        result = _client(_FakeProvider("정상 응답")).complete("prompt")

    assert result == "정상 응답"
    assert observer.stats().attempted == 2
    assert observer.stats().failed == 2
