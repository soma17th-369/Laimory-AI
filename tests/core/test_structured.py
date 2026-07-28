"""구조화 출력 골격(structured output) 단위 테스트.

provider 무관 검증·재시도 로직(`run_structured`), JSON 추출, provider 기본 hook,
FakeLLM 의 `complete_structured` 가 실제 클라이언트와 같은 경로를 타는지 검증한다.
실제 LLM 을 호출하지 않는다.
"""

import pytest
from pydantic import Field, model_validator

from app.core.llm import LLMProvider
from app.core.structured import (
    StructuredOutputError,
    extract_json_object,
    run_structured,
    schema_hint,
    to_strict_schema,
)
from app.schemas.common import CamelModel
from tests.fixtures.fake_llm import FakeLLM


class _Sample(CamelModel):
    """검증 스펙을 대표하는 스키마: alias·min_length·ge/le·optional·교차검증."""

    title: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    place_label: str | None = Field(default=None, alias="placeLabel")
    start_idx: int = Field(alias="startIdx")
    end_idx: int = Field(alias="endIdx")

    @model_validator(mode="after")
    def _order(self) -> "_Sample":
        if self.end_idx < self.start_idx:
            raise ValueError("endIdx must be >= startIdx")
        return self


_VALID = '{"title": "점심", "score": 0.8, "startIdx": 1, "endIdx": 2}'
# score 범위 초과: provider 형태는 맞지만 우리 Pydantic 값 검증이 잡아야 한다.
_BAD_SCORE = '{"title": "점심", "score": 2.0, "startIdx": 1, "endIdx": 2}'
# 교차검증 위반: end < start.
_BAD_ORDER = '{"title": "점심", "score": 0.5, "startIdx": 5, "endIdx": 1}'


class _StubProvider(LLMProvider):
    """자격증명/SDK 없이 `complete` 만 canned 응답으로 흉내내는 provider."""

    name = "stub"
    requires_api_key = False

    def __init__(self, responses: list[str]) -> None:
        # base __init__ 의 자격증명/모델 검증과 SDK 생성을 건너뛴다.
        self._responses = list(responses)
        self._i = 0
        self.prompts: list[str] = []
        self.model = "stub-model"
        self.api_key = ""
        self.client = None

    def _build_client(self):
        return None

    def complete(self, prompt, *, system=None, temperature=0.7, **kwargs) -> str:
        self.prompts.append(prompt)
        response = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return response


# --- extract_json_object -----------------------------------------------------


def test_extract_strips_code_fence():
    text = '```json\n{"a": 1}\n```'
    assert extract_json_object(text) == '{"a": 1}'


def test_extract_strips_surrounding_prose():
    text = '결과입니다: {"a": 1} 이상입니다.'
    assert extract_json_object(text) == '{"a": 1}'


def test_extract_without_object_raises():
    with pytest.raises(StructuredOutputError):
        extract_json_object("JSON 이 없습니다")


# --- run_structured ----------------------------------------------------------


def test_run_structured_returns_validated_model():
    calls: list[str] = []

    def fn(prompt, schema, *, system=None, temperature=0.7, **kwargs):
        calls.append(prompt)
        return _VALID

    result = run_structured(fn, "요청", _Sample)
    assert isinstance(result, _Sample)
    assert result.title == "점심" and result.score == 0.8
    assert len(calls) == 1


def test_run_structured_retries_then_succeeds():
    responses = [_BAD_SCORE, _VALID]
    prompts: list[str] = []

    def fn(prompt, schema, *, system=None, temperature=0.7, **kwargs):
        prompts.append(prompt)
        return responses[min(len(prompts) - 1, len(responses) - 1)]

    result = run_structured(fn, "요청", _Sample, max_repairs=1)
    assert result.score == 0.8
    assert len(prompts) == 2
    # 재시도 프롬프트에는 검증 오류가 실려야 한다.
    assert "검증 오류" in prompts[1]


def test_run_structured_exhausts_and_raises():
    def fn(prompt, schema, *, system=None, temperature=0.7, **kwargs):
        return _BAD_ORDER

    with pytest.raises(StructuredOutputError) as exc:
        run_structured(fn, "요청", _Sample, max_repairs=1)
    assert exc.value.errors is not None  # 마지막 검증 오류를 담는다


def test_run_structured_forwards_system_and_temperature():
    seen: dict = {}

    def fn(prompt, schema, *, system=None, temperature=0.7, **kwargs):
        seen["system"] = system
        seen["temperature"] = temperature
        return _VALID

    run_structured(fn, "요청", _Sample, system="지시", temperature=0.2)
    assert seen == {"system": "지시", "temperature": 0.2}


# --- LLMProvider 기본 hook ----------------------------------------------------


def test_provider_structured_passthrough_and_validates():
    provider = _StubProvider([_VALID])
    result = provider.complete_structured("요청", _Sample, temperature=0.2)
    assert isinstance(result, _Sample)
    # 기본 hook 은 프롬프트를 건드리지 않고 그대로 통과시킨다(형식 지시는 호출자 몫).
    assert provider.prompts[0] == "요청"


def test_schema_hint_uses_field_aliases():
    hint = schema_hint(_Sample)
    assert "placeLabel" in hint and "startIdx" in hint and "endIdx" in hint


# --- to_strict_schema --------------------------------------------------------


def test_to_strict_schema_forces_required_and_strips_value_constraints():
    schema = to_strict_schema(_Sample)
    assert schema is not None
    assert schema["additionalProperties"] is False
    # optional 을 포함한 모든 property 가 required 가 된다(optional 은 nullable).
    assert set(schema["required"]) == {
        "title",
        "score",
        "placeLabel",
        "startIdx",
        "endIdx",
    }
    # 값 제약은 떼어낸다 — 그건 이후 Pydantic 이 검증한다.
    assert "minimum" not in schema["properties"]["score"]
    assert "minLength" not in schema["properties"]["title"]


def test_to_strict_schema_returns_none_for_free_form_object():
    from typing import Any

    class _FreeForm(CamelModel):
        name: str
        args: dict[str, Any]

    # dict[str, Any] 는 properties 가 없는 자유형 object 라 strict 로 표현 못 한다.
    assert to_strict_schema(_FreeForm) is None


def test_provider_structured_retries_on_validation_error():
    provider = _StubProvider([_BAD_SCORE, _VALID])
    result = provider.complete_structured("요청", _Sample, max_repairs=1)
    assert result.score == 0.8
    assert len(provider.prompts) == 2


# --- FakeLLM.complete_structured ---------------------------------------------


def test_fake_llm_structured_validates_canned_json():
    llm = FakeLLM([_VALID])
    result = llm.complete_structured("요청", _Sample)
    assert isinstance(result, _Sample)
    assert llm.calls[0].prompt == "요청"


def test_fake_llm_structured_retries_then_succeeds():
    llm = FakeLLM([_BAD_ORDER, _VALID])
    result = llm.complete_structured("요청", _Sample, max_repairs=1)
    assert result.title == "점심"
    assert len(llm.calls) == 2
