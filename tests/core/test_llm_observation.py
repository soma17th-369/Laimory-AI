"""LLM provider 경계의 토큰 정규화와 실패 기록 검증.

- `_usage_detail` 은 provider 응답값만 담고, 없는 값은 생략한다(추정 금지).
- provider.complete 는 `complete()->str` 반환을 유지하면서 토큰 사용량을 구조화
  로그로 남기고, 실패는 errorCode 와 함께 남긴다.
- 프롬프트·응답 본문은 운영 로그로 나가지 않는다(그쪽은 Langfuse 담당, 이슈 #47).
"""

import json
import logging
from types import SimpleNamespace

import pytest

from app.core.error_codes import ErrorCode
from app.core.execution_context import ExecutionStage
from app.core.llm import BedrockProvider, GeminiProvider, OpenAIProvider


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


def test_openai_langfuse_usage_uses_exclusive_buckets() -> None:
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        prompt_tokens_details=SimpleNamespace(cached_tokens=70),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=15),
    )
    provider = object.__new__(OpenAIProvider)

    assert provider._langfuse_usage_detail(SimpleNamespace(usage=usage)) == {
        "input": 30,
        "input_cached_tokens": 70,
        "output": 25,
        "output_reasoning_tokens": 15,
    }


def test_gemini_usage_detail() -> None:
    usage = SimpleNamespace(
        prompt_token_count=11,
        candidates_token_count=22,
        total_token_count=33,
        cached_content_token_count=5,
    )
    detail = GeminiProvider._usage_detail(SimpleNamespace(usage_metadata=usage))
    assert detail == {"input": 11, "output": 22, "total": 33, "cached": 5}


def test_gemini_langfuse_usage_uses_exclusive_buckets() -> None:
    usage = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=20,
        total_token_count=135,
        cached_content_token_count=60,
        thoughts_token_count=10,
        tool_use_prompt_token_count=5,
    )
    provider = object.__new__(GeminiProvider)

    assert provider._langfuse_usage_detail(
        SimpleNamespace(usage_metadata=usage)
    ) == {
        "input": 40,
        "input_cached_tokens": 60,
        "input_tool_tokens": 5,
        "output": 20,
        "output_reasoning_tokens": 10,
    }


def test_bedrock_usage_detail() -> None:
    response = {"usage": {"inputTokens": 5, "outputTokens": 6, "totalTokens": 11}}
    assert BedrockProvider._usage_detail(response) == {
        "input": 5,
        "output": 6,
        "total": 11,
    }


def test_bedrock_langfuse_usage_preserves_non_overlapping_cache_buckets() -> None:
    response = {
        "usage": {
            "inputTokens": 5,
            "cacheReadInputTokens": 7,
            "cacheWriteInputTokens": 3,
            "outputTokens": 6,
            "totalTokens": 11,
        }
    }
    provider = object.__new__(BedrockProvider)

    assert provider._langfuse_usage_detail(response) == {
        "input": 5,
        "cache_read_input_tokens": 7,
        "cache_write_input_tokens": 3,
        "output": 6,
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


def test_openai_complete_returns_text_and_logs_token_usage(caplog) -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    provider = _openai_provider(_FakeOpenAIClient(response=_fake_response("hello", usage)))

    with caplog.at_level(logging.DEBUG, logger="app.core.llm"):
        text = provider.complete("hi", system="sys")

    assert text == "hello"  # 공개 계약(str 반환) 유지
    usage_record = next(
        record for record in caplog.records if record.getMessage() == "LLM 토큰 사용량"
    )
    assert usage_record.fields == {
        "provider": "openai",
        "model": "gpt-test",
        "inputTokens": 10,
        "outputTokens": 20,
    }


def test_llm_logs_never_carry_prompt_or_response_bodies(caplog) -> None:
    """프롬프트·응답 본문은 Langfuse 로만 나간다(운영 로그로 복제하지 않는다)."""

    usage = SimpleNamespace(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    provider = _openai_provider(
        _FakeOpenAIClient(response=_fake_response("응답-본문-비밀", usage))
    )

    with caplog.at_level(logging.DEBUG, logger="app.core.llm"):
        provider.complete("프롬프트-본문-비밀", system="시스템-본문-비밀")

    logged = json.dumps(
        [
            {"message": record.getMessage(), "fields": getattr(record, "fields", {})}
            for record in caplog.records
        ],
        ensure_ascii=False,
        default=str,
    )
    assert "프롬프트-본문-비밀" not in logged
    assert "시스템-본문-비밀" not in logged
    assert "응답-본문-비밀" not in logged


def test_openai_complete_reports_failure_code_and_reraises(caplog) -> None:
    provider = _openai_provider(_FakeOpenAIClient(exc=RuntimeError("boom")))

    with caplog.at_level(logging.WARNING, logger="app.core.llm"):
        with pytest.raises(RuntimeError):
            provider.complete("hi")

    failures = [
        record for record in caplog.records if record.getMessage() == "LLM 호출 실패"
    ]
    assert len(failures) == 1
    fields = failures[0].fields
    assert fields["errorCode"] == int(ErrorCode.LLM_CALL_FAILED)
    assert fields["stage"] == ExecutionStage.LLM.value
    assert fields["provider"] == "openai"
    assert fields["model"] == "gpt-test"
    assert fields["durationMs"] >= 0
    # providerVersion 은 설치된 openai SDK 버전에서 온다.
    assert fields["providerVersion"] is not None


def test_openai_response_parsing_failure_is_reported(caplog) -> None:
    provider = _openai_provider(
        _FakeOpenAIClient(response=SimpleNamespace(choices=[], usage=None))
    )

    with caplog.at_level(logging.WARNING, logger="app.core.llm"):
        with pytest.raises(IndexError):
            provider.complete("hi")

    assert any(
        record.getMessage() == "LLM 호출 실패"
        and record.fields["errorCode"] == int(ErrorCode.LLM_CALL_FAILED)
        for record in caplog.records
    )


def test_llm_call_works_without_execution_context() -> None:
    # 실행 컨텍스트가 없어도(스크립트 실행) provider.complete 는 그대로 동작한다.
    usage = SimpleNamespace(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    provider = _openai_provider(_FakeOpenAIClient(response=_fake_response("ok", usage)))
    assert provider.complete("hi") == "ok"
