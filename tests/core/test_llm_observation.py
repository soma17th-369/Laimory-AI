"""LLM provider 경계의 관측(PROMPT/RESPONSE/FAILED)과 토큰 정규화 검증.

- `_usage_detail` 은 provider 응답값만 담고, 없는 값은 생략한다(추정 금지).
- provider.complete 는 `complete()->str` 반환을 유지하면서 PROMPT/RESPONSE(+토큰·
  duration·provider/model/providerVersion)를, 실패 시 FAILED 를 emit 한다.
"""

import json
from types import SimpleNamespace

import pytest

from app.core.error_codes import ErrorCode
from app.core.llm import BedrockProvider, GeminiProvider, OpenAIProvider
from app.core.observability import (
    InMemoryObservationSink,
    ObservationEventType,
    ObservationStage,
    Observer,
    observation_context,
)


# --- _usage_detail: provider 응답값만, 없는 값은 생략 -------------------------


def test_openai_usage_detail_normalizes_available_fields() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
    )
    detail = OpenAIProvider._usage_detail(SimpleNamespace(usage=usage))
    assert detail == {"input": 10, "output": 20, "total": 30, "cached": 4, "reasoning": 3}


def test_openai_usage_detail_omits_missing_fields() -> None:
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
    detail = OpenAIProvider._usage_detail(SimpleNamespace(usage=usage))
    # total/cached/reasoning 는 응답에 없으므로 생략된다.
    assert detail == {"input": 10, "output": 20}


def test_openai_usage_detail_without_usage_is_empty() -> None:
    assert OpenAIProvider._usage_detail(SimpleNamespace()) == {}


def test_gemini_usage_detail() -> None:
    usage = SimpleNamespace(
        prompt_token_count=11,
        candidates_token_count=22,
        total_token_count=33,
        cached_content_token_count=5,
    )
    detail = GeminiProvider._usage_detail(SimpleNamespace(usage_metadata=usage))
    assert detail == {"input": 11, "output": 22, "total": 33, "cached": 5}


def test_bedrock_usage_detail() -> None:
    response = {"usage": {"inputTokens": 5, "outputTokens": 6, "totalTokens": 11}}
    assert BedrockProvider._usage_detail(response) == {
        "input": 5,
        "output": 6,
        "total": 11,
    }


# --- 실제 provider.complete 의 emit (OpenAI 대표, SDK 는 fake) ----------------


class _FakeCompletions:
    def __init__(self, response=None, exc=None) -> None:
        self._response = response
        self._exc = exc

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeOpenAIClient:
    def __init__(self, response=None, exc=None) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(response, exc))


def _openai_provider(client) -> OpenAIProvider:
    # __init__ 은 실제 SDK·자격증명을 요구하므로 우회하고 필요한 속성만 채운다.
    provider = object.__new__(OpenAIProvider)
    provider.model = "gpt-test"
    provider.api_key = "x"
    provider.client = client
    return provider


def _fake_response(content: str, usage) -> SimpleNamespace:
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_openai_complete_emits_prompt_and_response_with_tokens() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    provider = _openai_provider(_FakeOpenAIClient(response=_fake_response("hello", usage)))

    sink = InMemoryObservationSink()
    with observation_context("task-llm", Observer(sink)):
        text = provider.complete("hi", system="sys")

    assert text == "hello"  # 공개 계약(str 반환) 유지
    prompt_ev = next(e for e in sink.events if e.event_type is ObservationEventType.PROMPT)
    response_ev = next(
        e for e in sink.events if e.event_type is ObservationEventType.RESPONSE
    )
    assert prompt_ev.stage is ObservationStage.LLM
    assert prompt_ev.provider == "openai" and prompt_ev.model == "gpt-test"
    assert prompt_ev.payload == {
        "prompt": "hi",
        "system": "sys",
        "temperature": 0.7,
    }
    assert response_ev.input_tokens == 10
    assert response_ev.output_tokens == 20
    assert response_ev.total_tokens == 30
    assert response_ev.duration_ms is not None and response_ev.duration_ms >= 0
    assert response_ev.payload == {"response": "hello"}
    # providerVersion 은 설치된 openai SDK 버전에서 온다.
    assert response_ev.provider_version is not None


def test_openai_complete_emits_failed_and_reraises() -> None:
    provider = _openai_provider(_FakeOpenAIClient(exc=RuntimeError("boom")))

    sink = InMemoryObservationSink()
    with observation_context("task-llm", Observer(sink)):
        with pytest.raises(RuntimeError):
            provider.complete("hi")

    failed = [e for e in sink.events if e.event_type is ObservationEventType.FAILED]
    assert len(failed) == 1
    assert failed[0].stage is ObservationStage.LLM
    assert failed[0].provider == "openai"
    assert failed[0].payload == {
        "errorCode": int(ErrorCode.LLM_CALL_FAILED),
        "errorType": "RuntimeError",
    }
    assert "boom" not in json.dumps(failed[0].payload)


def test_openai_response_parsing_failure_is_observed() -> None:
    provider = _openai_provider(
        _FakeOpenAIClient(response=SimpleNamespace(choices=[], usage=None))
    )

    sink = InMemoryObservationSink()
    with observation_context("task-llm", Observer(sink)):
        with pytest.raises(IndexError):
            provider.complete("hi")

    assert any(
        event.event_type is ObservationEventType.FAILED
        and event.stage is ObservationStage.LLM
        for event in sink.events
    )


def test_llm_emit_is_noop_without_context() -> None:
    # 관측 컨텍스트가 없으면 provider.complete 는 그대로 동작하고 아무것도 남기지 않는다.
    usage = SimpleNamespace(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    provider = _openai_provider(_FakeOpenAIClient(response=_fake_response("ok", usage)))
    assert provider.complete("hi") == "ok"
