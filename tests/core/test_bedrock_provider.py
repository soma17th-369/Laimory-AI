"""Bedrock Provider 단위 테스트.

AWS 계정/자격증명 없이 실행 가능하도록 boto3 client 를 mock 한다.
converse 호출 인자(요청 구조), 텍스트 추출, 토큰 사용량 로그, 자격증명 전달,
에러 처리를 검증한다.
"""

import logging

import pytest

from app.core import llm as llm_module
from app.core.error_codes import ErrorCode
from app.core.llm import (
    BedrockProvider,
    ImageInput,
    available_providers,
    get_provider,
)
from app.core.structured import ProviderStructuredOutputError
from app.schemas import AgentEventResult


class FakeBedrockClient:
    """boto3 bedrock-runtime client 를 흉내낸다.

    converse 호출 인자를 기록하고 정해진 응답을 돌려준다.
    """

    def __init__(self, response: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or {
            "output": {"message": {"content": [{"text": "안녕"}]}},
            "usage": {"inputTokens": 12, "outputTokens": 5},
        }

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _make_provider(monkeypatch, response=None, capture=None):
    """boto3.client 를 FakeBedrockClient 로 바꿔치기한 BedrockProvider 를 만든다.

    capture 를 주면 boto3.client 로 넘어간 (service, kwargs) 를 담아 클라이언트
    생성 인자를 검증할 수 있다.
    """

    fake = FakeBedrockClient(response=response)
    monkeypatch.setattr(llm_module.settings, "bedrock_aws_profile", "")

    def fake_client(service_name, **kwargs):
        if capture is not None:
            capture["service"] = service_name
            capture["kwargs"] = kwargs
        return fake

    monkeypatch.setattr("boto3.client", fake_client)
    provider = BedrockProvider(model="amazon.nova-lite-v1:0")
    return provider, fake


def test_bedrock_registered():
    assert "bedrock" in available_providers()


def test_requires_no_api_key(monkeypatch):
    # api_key 없이도(모델만 있으면) 생성된다. bedrock 은 자격증명 체인으로 인증한다.
    provider, _ = _make_provider(monkeypatch)
    assert provider.requires_api_key is False
    assert provider.supports_vision is True
    assert provider.api_key == ""  # bedrock_api_key 필드가 없어 빈 값


def test_empty_model_raises(monkeypatch):
    monkeypatch.setattr(llm_module.settings, "bedrock_model", "")
    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeBedrockClient())
    with pytest.raises(ValueError):
        BedrockProvider(model="")


def test_client_built_with_region_only(monkeypatch):
    # AgentCore 처럼 프로필이 없으면 기본 체인(실행 역할)에 맡긴다.
    monkeypatch.setattr(llm_module.settings, "bedrock_aws_profile", "")
    monkeypatch.setattr(llm_module.settings, "bedrock_region", "ap-northeast-2")
    capture: dict = {}
    _make_provider(monkeypatch, capture=capture)
    assert capture["service"] == "bedrock-runtime"
    assert capture["kwargs"] == {"region_name": "ap-northeast-2"}


def test_client_built_from_named_local_profile(monkeypatch):
    fake = FakeBedrockClient()
    capture: dict = {}

    class FakeSession:
        def client(self, service_name, **kwargs):
            capture["service"] = service_name
            capture["kwargs"] = kwargs
            return fake

    def fake_session(*, profile_name):
        capture["profile_name"] = profile_name
        return FakeSession()

    monkeypatch.setattr(llm_module.settings, "app_env", "local")
    monkeypatch.setattr(llm_module.settings, "bedrock_aws_profile", "laimory-bedrock")
    monkeypatch.setattr(llm_module.settings, "bedrock_region", "ap-northeast-2")
    monkeypatch.setattr("boto3.Session", fake_session)

    BedrockProvider(model="amazon.nova-lite-v1:0")

    assert capture == {
        "profile_name": "laimory-bedrock",
        "service": "bedrock-runtime",
        "kwargs": {"region_name": "ap-northeast-2"},
    }


def test_deployment_ignores_local_profile_and_uses_default_chain(monkeypatch):
    fake = FakeBedrockClient()
    capture: dict = {}

    def fake_client(service_name, **kwargs):
        capture["service"] = service_name
        capture["kwargs"] = kwargs
        return fake

    def fail_session(**kwargs):
        pytest.fail(f"배포 환경에서 로컬 AWS 프로필을 사용했습니다: {kwargs}")

    monkeypatch.setattr(llm_module.settings, "app_env", "prod")
    monkeypatch.setattr(llm_module.settings, "bedrock_aws_profile", "laimory-bedrock")
    monkeypatch.setattr(llm_module.settings, "bedrock_region", "ap-northeast-2")
    monkeypatch.setattr("boto3.Session", fail_session)
    monkeypatch.setattr("boto3.client", fake_client)

    BedrockProvider(model="amazon.nova-lite-v1:0")

    assert capture == {
        "service": "bedrock-runtime",
        "kwargs": {"region_name": "ap-northeast-2"},
    }


def test_complete_builds_converse_request_and_returns_text(monkeypatch):
    provider, fake = _make_provider(monkeypatch)
    out = provider.complete("질문", system="지시", temperature=0.3)
    assert out == "안녕"
    call = fake.calls[0]
    assert call["modelId"] == "amazon.nova-lite-v1:0"
    assert call["messages"] == [{"role": "user", "content": [{"text": "질문"}]}]
    assert call["system"] == [{"text": "지시"}]
    # maxTokens 는 항상 실린다(#98). 상한이 없으면 Nova 가 tool call 형식을 깨뜨린다.
    assert call["inferenceConfig"] == {
        "temperature": 0.3,
        "maxTokens": llm_module.settings.bedrock_max_tokens,
    }


def test_complete_without_system_omits_system(monkeypatch):
    provider, fake = _make_provider(monkeypatch)
    provider.complete("질문")
    assert "system" not in fake.calls[0]


def test_complete_forwards_converse_options(monkeypatch):
    provider, fake = _make_provider(monkeypatch)

    provider.complete(
        "질문",
        temperature=0.3,
        maxTokens=256,
        topP=0.8,
        stopSequences=["END"],
        requestMetadata={"taskId": "task-1"},
    )

    call = fake.calls[0]
    assert call["inferenceConfig"] == {
        "temperature": 0.3,
        "maxTokens": 256,
        "topP": 0.8,
        "stopSequences": ["END"],
    }
    assert call["requestMetadata"] == {"taskId": "task-1"}


def test_complete_accepts_boto3_inference_config(monkeypatch):
    provider, fake = _make_provider(monkeypatch)

    provider.complete(
        "질문",
        temperature=0.7,
        inferenceConfig={"temperature": 0.1, "maxTokens": 128},
    )

    assert fake.calls[0]["inferenceConfig"] == {
        "temperature": 0.1,
        "maxTokens": 128,
    }


def test_complete_joins_multiple_text_blocks(monkeypatch):
    response = {
        "output": {"message": {"content": [{"text": "가"}, {"text": "나"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }
    provider, _ = _make_provider(monkeypatch, response=response)
    assert provider.complete("x") == "가나"


def test_complete_with_images_attaches_image_blocks(monkeypatch):
    provider, fake = _make_provider(monkeypatch)
    out = provider.complete_with_images(
        "설명", [ImageInput(data=b"\xff\xd8\xff", mime_type="image/jpeg")]
    )
    assert out == "안녕"
    content = fake.calls[0]["messages"][0]["content"]
    assert content[0] == {"text": "설명"}
    assert content[1]["image"]["format"] == "jpeg"
    assert content[1]["image"]["source"]["bytes"] == b"\xff\xd8\xff"


def _usage_fields(caplog) -> dict:
    record = next(
        r for r in caplog.records if r.getMessage() == "LLM 토큰 사용량"
    )
    return record.fields


def test_logs_token_usage(monkeypatch, caplog):
    provider, _ = _make_provider(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="app.core.llm"):
        provider.complete("x")

    fields = _usage_fields(caplog)
    assert fields["provider"] == "bedrock"
    assert fields["inputTokens"] == 12
    assert fields["outputTokens"] == 5


def test_usage_missing_omits_token_fields(monkeypatch, caplog):
    """usage 를 주지 않는 응답이면 토큰 필드를 빼고 남긴다(추정하지 않는다)."""

    response = {"output": {"message": {"content": [{"text": "hi"}]}}}  # usage 없음
    provider, _ = _make_provider(monkeypatch, response=response)
    with caplog.at_level(logging.DEBUG, logger="app.core.llm"):
        provider.complete("x")

    fields = _usage_fields(caplog)
    assert "inputTokens" not in fields
    assert "outputTokens" not in fields
    assert fields["provider"] == "bedrock"


def test_image_format_mapping():
    assert BedrockProvider._image_format("image/jpeg") == "jpeg"
    assert BedrockProvider._image_format("image/jpg") == "jpeg"
    assert BedrockProvider._image_format("image/png") == "png"
    assert BedrockProvider._image_format("image/webp") == "webp"
    assert BedrockProvider._image_format("") == "jpeg"


def test_unsupported_provider_raises():
    with pytest.raises(ValueError):
        get_provider("does-not-exist")


# --- #98 구조화 출력 방어 -------------------------------------------------------


def _structured_response(*, stop_reason, content):
    return {
        "output": {"message": {"content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
    }


def test_default_max_tokens_comes_from_settings(monkeypatch):
    monkeypatch.setattr(llm_module.settings, "bedrock_max_tokens", 12345)
    provider, fake = _make_provider(monkeypatch)
    provider.complete("질문")
    assert fake.calls[0]["inferenceConfig"]["maxTokens"] == 12345


def test_caller_max_tokens_wins_over_default(monkeypatch):
    monkeypatch.setattr(llm_module.settings, "bedrock_max_tokens", 12345)
    provider, fake = _make_provider(monkeypatch)
    provider.complete("질문", maxTokens=77)
    assert fake.calls[0]["inferenceConfig"]["maxTokens"] == 77


def test_structured_call_pins_temperature_to_zero(monkeypatch):
    """구조화 호출은 호출자 temperature 를 무시하고 0 으로 잠근다."""

    response = _structured_response(
        stop_reason="tool_use",
        content=[{"toolUse": {"name": "AgentEventResult", "input": {"candidates": []}}}],
    )
    provider, fake = _make_provider(monkeypatch, response=response)
    provider.complete_json("질문", AgentEventResult, temperature=0.9)
    assert fake.calls[0]["inferenceConfig"]["temperature"] == 0.0


def test_malformed_tool_use_raises_instead_of_empty_string(monkeypatch):
    """빈 content 를 성공으로 위장하지 않는다 — 이슈 #98 의 핵심."""

    response = _structured_response(stop_reason="malformed_tool_use", content=[])
    provider, _ = _make_provider(monkeypatch, response=response)

    with pytest.raises(ProviderStructuredOutputError) as exc_info:
        provider.complete_json("질문", AgentEventResult)

    fields = exc_info.value.trace_fields()
    assert fields["stopReason"] == "malformed_tool_use"
    assert fields["contentBlockKinds"] == []


def test_end_turn_without_tool_use_raises(monkeypatch):
    """도구 호출을 강제했는데 모델이 부르지 않은 경우도 실패다."""

    response = _structured_response(stop_reason="end_turn", content=[])
    provider, _ = _make_provider(monkeypatch, response=response)
    with pytest.raises(ProviderStructuredOutputError):
        provider.complete_json("질문", AgentEventResult)


def test_unexpected_tool_name_raises(monkeypatch):
    response = _structured_response(
        stop_reason="tool_use",
        content=[{"toolUse": {"name": "SomethingElse", "input": {}}}],
    )
    provider, _ = _make_provider(monkeypatch, response=response)
    with pytest.raises(ProviderStructuredOutputError):
        provider.complete_json("질문", AgentEventResult)


def test_text_answer_is_returned_for_downstream_parsing(monkeypatch):
    """모델이 도구 대신 텍스트로 답하면 내용이 있으므로 그대로 넘긴다."""

    response = _structured_response(
        stop_reason="end_turn",
        content=[{"text": '{"candidates": []}'}],
    )
    provider, _ = _make_provider(monkeypatch, response=response)
    assert provider.complete_json("질문", AgentEventResult) == '{"candidates": []}'


def test_structured_failure_is_not_reported_as_llm_call_failure(monkeypatch, caplog):
    """호출은 200 으로 성공했다. 1203(호출 실패)이 아니라 1202 로 남아야 한다."""

    response = _structured_response(stop_reason="malformed_tool_use", content=[])
    provider, _ = _make_provider(monkeypatch, response=response)

    # report_error 의 기본 레벨은 WARNING 이다.
    with caplog.at_level(logging.WARNING, logger="app.core.llm"):
        with pytest.raises(ProviderStructuredOutputError):
            provider.complete_json("질문", AgentEventResult)

    codes = {
        record.fields.get("errorCode")
        for record in caplog.records
        if hasattr(record, "fields")
    }
    assert int(ErrorCode.STRUCTURED_OUTPUT_INVALID) in codes
    assert int(ErrorCode.LLM_CALL_FAILED) not in codes
