"""provider 네이티브 구조화 출력 단위 테스트.

OpenAI/Gemini 가 ``complete_structured`` 에서 **스키마 기반 강제**(필수 필드·enum)를
실제 SDK 호출까지 전달하고, 응답을 우리 Pydantic 으로 검증해 돌려주는지 확인한다.
자유형 object 처럼 strict 로 표현 못 하는 스키마는 JSON 모드로 떨어진다. Bedrock/Nova 는
tool-use 로 스키마를 강제한다. 실제 API 는 호출하지 않는다.
"""

from enum import Enum
from types import SimpleNamespace
from typing import Any

from pydantic import Field

from app.core import llm as llm_module
from app.core.llm import BedrockProvider, GeminiProvider, OpenAIProvider
from app.schemas.common import CamelModel


class _Kind(str, Enum):
    A = "A"
    B = "B"


class _Doc(CamelModel):
    name: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    kind: _Kind


class _FreeForm(CamelModel):
    name: str
    args: dict[str, Any]  # 자유형 object → strict 불가


_VALID = '{"name": "점심", "score": 0.5, "kind": "A"}'


# --- OpenAI ------------------------------------------------------------------


class _FakeOpenAIClient:
    def __init__(self, content: str) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )
        self._content = content

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _make_openai(monkeypatch, content: str):
    monkeypatch.setattr(llm_module.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(llm_module.settings, "openai_model", "gpt-x")
    fake = _FakeOpenAIClient(content)
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: fake)
    return OpenAIProvider(), fake


def test_openai_structured_uses_json_schema_strict(monkeypatch):
    provider, fake = _make_openai(monkeypatch, _VALID)
    result = provider.complete_structured("질문", _Doc, temperature=0.2)
    assert isinstance(result, _Doc)
    assert result.kind is _Kind.A

    fmt = fake.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    # 필수 필드가 전부 required 로, 추가 필드는 금지로 강제된다.
    assert set(schema["required"]) == {"name", "score", "kind"}
    assert schema["additionalProperties"] is False


def test_openai_free_form_schema_falls_back_to_json_object(monkeypatch):
    provider, fake = _make_openai(monkeypatch, '{"name": "x", "args": {"a": 1}}')
    result = provider.complete_structured("질문", _FreeForm)
    assert isinstance(result, _FreeForm)
    # dict[str, Any] 는 strict 불가라 json_object 모드로 떨어진다.
    assert fake.calls[0]["response_format"] == {"type": "json_object"}


def test_openai_structured_retries_on_invalid_then_valid(monkeypatch):
    # 첫 응답은 score 범위 초과(값 규칙 위반), 두 번째는 정상.
    provider, fake = _make_openai(monkeypatch, _VALID)

    responses = ['{"name": "점심", "score": 9.9, "kind": "A"}', _VALID]

    def create(**kwargs):
        fake.calls.append(kwargs)
        content = responses[min(len(fake.calls) - 1, len(responses) - 1)]
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    fake.chat.completions.create = create
    result = provider.complete_structured("질문", _Doc, max_repairs=1)
    assert result.score == 0.5
    assert len(fake.calls) == 2


# --- Gemini ------------------------------------------------------------------


class _FakeGeminiClient:
    def __init__(self, text: str) -> None:
        self.calls: list[dict] = []
        self.models = SimpleNamespace(generate_content=self._generate)
        self._text = text

    def _generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self._text, usage_metadata=None)


def _make_gemini(monkeypatch, text: str):
    monkeypatch.setattr(llm_module.settings, "gemini_api_key", "g-test")
    monkeypatch.setattr(llm_module.settings, "gemini_model", "gemini-x")
    fake = _FakeGeminiClient(text)
    monkeypatch.setattr("google.genai.Client", lambda **kwargs: fake)
    return GeminiProvider(), fake


def test_gemini_structured_sets_response_schema(monkeypatch):
    provider, fake = _make_gemini(monkeypatch, _VALID)
    result = provider.complete_structured("질문", _Doc)
    assert isinstance(result, _Doc)
    config = fake.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    # 스키마를 provider 에 넘겨 필수·enum 을 강제하게 한다.
    assert config.response_schema is not None


# --- Bedrock (tool-use 강제) --------------------------------------------------


class _FakeBedrockClient:
    def __init__(self, *, tool_input: dict | None = None, text: str | None = None) -> None:
        self.calls: list[dict] = []
        self._tool_input = tool_input
        self._text = text

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._tool_input is not None:
            content = [{"toolUse": {"name": "_Doc", "input": self._tool_input}}]
        else:
            content = [{"text": self._text}]
        return {
            "output": {"message": {"content": content}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


def _make_bedrock(monkeypatch, fake):
    monkeypatch.setattr(llm_module.settings, "bedrock_aws_profile", "")
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    return BedrockProvider(model="amazon.nova-lite-v1:0")


def test_bedrock_structured_forces_tool_use(monkeypatch):
    fake = _FakeBedrockClient(tool_input={"name": "점심", "score": 0.5, "kind": "A"})
    provider = _make_bedrock(monkeypatch, fake)

    result = provider.complete_structured("질문", _Doc, temperature=0.2)
    assert isinstance(result, _Doc)
    assert result.name == "점심"

    tool_config = fake.calls[0]["toolConfig"]
    assert tool_config["toolChoice"] == {"tool": {"name": "_Doc"}}
    spec = tool_config["tools"][0]["toolSpec"]
    assert spec["name"] == "_Doc"
    assert "json" in spec["inputSchema"]  # 스키마를 도구 inputSchema 로 실었다


def test_bedrock_complete_json_without_schema_is_passthrough(monkeypatch):
    fake = _FakeBedrockClient(text=_VALID)
    provider = _make_bedrock(monkeypatch, fake)

    out = provider.complete_json("질문")
    assert out == _VALID
    # 스키마가 없으면 tool-use 를 쓰지 않고 그대로 converse 한다.
    assert "toolConfig" not in fake.calls[0]
    assert fake.calls[0]["messages"][0]["content"] == [{"text": "질문"}]
