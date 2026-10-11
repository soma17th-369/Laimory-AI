"""OpenAI provider 요청 조립 단위 테스트 (#108).

GPT-5 계열 reasoning 모델은 **추론이 켜진 상태에서 `temperature` 를 받지 않는다.**
`OPENAI_MODEL_FAST=gpt-5.6-luna` 로 두면 호출자가 넘긴 0.2 가 그대로 나가
`400 Unsupported value: 'temperature' does not support 0.2 with this model` 이 났고,
Event Agent 가 이를 1204 warning 으로 삼켜 후보가 통째로 비었다.

여기서 확인하는 것은 provider 경계가 모델별로 `reasoning_effort` 를 싣고
`temperature` 를 뺄지 말지를 네 호출 경로(텍스트·이미지·JSON·Structured)에서 똑같이
판단하는지다. 실제 API 는 호출하지 않는다.
"""

from types import SimpleNamespace

import pytest
from pydantic import Field

from app.core import llm as llm_module
from app.core.llm import ImageInput, OpenAIProvider
from app.schemas.common import CamelModel


class _Doc(CamelModel):
    name: str = Field(min_length=1)


_VALID = '{"name": "점심"}'

#: 추론이 켜져 `temperature` 를 못 싣는 모델.
_REASONING_MODEL = "gpt-5.6-luna"
#: 추론이 꺼져 있어 `temperature` 가 그대로 나가는 모델.
_NON_REASONING_MODEL = "gpt-5.4-mini"
#: 표에 없는 모델. 동작이 예전과 같아야 한다.
_UNLISTED_MODEL = "gpt-4o"


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


def _make_openai(monkeypatch, model: str, content: str = _VALID):
    monkeypatch.setattr(llm_module.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(llm_module.settings, "openai_model", model)
    fake = _FakeOpenAIClient(content)
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: fake)
    return OpenAIProvider(), fake


def _image() -> ImageInput:
    return ImageInput(data=b"\xff\xd8\xff", mime_type="image/jpeg")


# --- 추론이 켜진 모델: temperature 를 싣지 않는다 ------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda provider: provider.complete("질문", temperature=0.2),
            id="text",
        ),
        pytest.param(
            lambda provider: provider.complete_with_images(
                "질문", [_image()], temperature=0.2
            ),
            id="images",
        ),
        pytest.param(
            lambda provider: provider.complete_json("질문", temperature=0.2),
            id="json",
        ),
        pytest.param(
            lambda provider: provider.complete_structured(
                "질문", _Doc, temperature=0.2
            ),
            id="structured",
        ),
    ],
)
def test_reasoning_model_omits_temperature_on_every_path(monkeypatch, call) -> None:
    """네 경로 모두 provider 경계에서 걸러진다.

    JSON·Structured 는 `complete()` 를 경유하므로 별도 분기가 없다. 그래도 함께 재는
    이유는 그 경유가 끊기면 조용히 400 이 돌아오기 때문이다.
    """

    provider, fake = _make_openai(monkeypatch, _REASONING_MODEL)

    call(provider)

    assert "temperature" not in fake.calls[0]
    assert fake.calls[0]["reasoning_effort"] == "low"


def test_reasoning_model_keeps_response_format(monkeypatch) -> None:
    """`temperature` 를 뺀다고 구조화 강제까지 사라지면 안 된다."""

    provider, fake = _make_openai(monkeypatch, _REASONING_MODEL)

    provider.complete_structured("질문", _Doc, temperature=0.2)

    fmt = fake.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert "temperature" not in fake.calls[0]


def test_dated_snapshot_id_is_treated_as_the_same_model(monkeypatch) -> None:
    """날짜 스냅샷 id 도 같은 모델이다.

    정확히 일치시키면 `gpt-5.6-luna-2026-05-01` 같은 id 를 놓쳐 운영에서 같은 400 이
    다시 난다. prefix 로 재는 이유가 이것이다.
    """

    provider, fake = _make_openai(monkeypatch, f"{_REASONING_MODEL}-2026-05-01")

    provider.complete("질문", temperature=0.2)

    assert "temperature" not in fake.calls[0]
    assert fake.calls[0]["reasoning_effort"] == "low"


# --- 추론이 꺼진 모델: 예전과 같이 temperature 를 싣는다 -----------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda provider: provider.complete("질문", temperature=0.2),
            id="text",
        ),
        pytest.param(
            lambda provider: provider.complete_with_images(
                "질문", [_image()], temperature=0.2
            ),
            id="images",
        ),
        pytest.param(
            lambda provider: provider.complete_json("질문", temperature=0.2),
            id="json",
        ),
        pytest.param(
            lambda provider: provider.complete_structured(
                "질문", _Doc, temperature=0.2
            ),
            id="structured",
        ),
    ],
)
def test_non_reasoning_model_still_sends_temperature(monkeypatch, call) -> None:
    provider, fake = _make_openai(monkeypatch, _NON_REASONING_MODEL)

    call(provider)

    assert fake.calls[0]["temperature"] == 0.2
    assert fake.calls[0]["reasoning_effort"] == "none"


def test_non_reasoning_model_pins_effort_explicitly(monkeypatch) -> None:
    """모델 기본값과 같은 `none` 을 굳이 명시한다.

    OpenAI 가 기본값을 바꾸면 우리가 고른 적 없는 추론이 켜지고, 그 순간
    `temperature` 가 조용히 거부된다. 명시해 두면 그 변화에 흔들리지 않는다.
    """

    provider, fake = _make_openai(monkeypatch, _NON_REASONING_MODEL)

    provider.complete("질문", temperature=0.0)

    assert fake.calls[0]["reasoning_effort"] == "none"
    assert fake.calls[0]["temperature"] == 0.0


# --- 표에 없는 모델: 동작이 예전과 같다 ----------------------------------------


def test_unlisted_model_behaves_as_before(monkeypatch) -> None:
    """표에 없는 모델에는 `reasoning_effort` 를 싣지 않는다.

    이 표는 우리가 실제로 쓰는 모델만 담는다. 모르는 모델에 추론 설정을 밀어넣으면
    추론을 지원하지 않는 모델에서 새 오류를 만든다.
    """

    provider, fake = _make_openai(monkeypatch, _UNLISTED_MODEL)

    provider.complete("질문", temperature=0.2)

    assert fake.calls[0]["temperature"] == 0.2
    assert "reasoning_effort" not in fake.calls[0]


def test_caller_kwargs_win_over_the_table(monkeypatch) -> None:
    """호출자가 명시적으로 준 값이 표보다 우선한다."""

    provider, fake = _make_openai(monkeypatch, _REASONING_MODEL)

    provider.complete("질문", temperature=0.2, reasoning_effort="high")

    assert fake.calls[0]["reasoning_effort"] == "high"


# --- 관측: 실제 요청에 실린 값만 남는다 ----------------------------------------


def test_generation_records_only_applied_parameters(monkeypatch) -> None:
    """요청에서 뺀 `temperature` 가 관측에 남으면 실효값처럼 보인다."""

    recorded: list[dict] = []

    def fake_trace(name, **kwargs):
        recorded.append(kwargs)
        from contextlib import nullcontext

        return nullcontext(None)

    provider, _ = _make_openai(monkeypatch, _REASONING_MODEL)
    monkeypatch.setattr(llm_module, "trace_observation", fake_trace)

    provider.complete("질문", temperature=0.2)

    assert recorded[0]["model_parameters"] == {"reasoningEffort": "low"}


def test_generation_keeps_temperature_when_it_is_applied(monkeypatch) -> None:
    recorded: list[dict] = []

    def fake_trace(name, **kwargs):
        recorded.append(kwargs)
        from contextlib import nullcontext

        return nullcontext(None)

    provider, _ = _make_openai(monkeypatch, _NON_REASONING_MODEL)
    monkeypatch.setattr(llm_module, "trace_observation", fake_trace)

    provider.complete("질문", temperature=0.2)

    assert recorded[0]["model_parameters"] == {
        "temperature": 0.2,
        "reasoningEffort": "none",
    }


# --- 표 자체의 자기 일관성 --------------------------------------------------


def test_reasoning_models_do_not_claim_temperature_support() -> None:
    """추론을 켠 항목은 `temperature` 를 받는다고 적혀 있으면 안 된다.

    두 매개변수를 따로 선언하니 표 안에서 어긋날 수 있고, 어긋나면 운영에서 400 이
    난다. 여기서 잡는다. 다만 이건 GPT-5 계열의 **현행 계약**을 재는 것이지 두 값이
    한 축이라는 뜻이 아니다 — 추론과 `temperature` 를 함께 받는 모델이 들어오면 그
    항목만 예외로 두거나 이 테스트를 지운다.
    """

    for prefix, params in OpenAIProvider._MODEL_PARAMS:
        if params.reasoning_effort in (None, "none"):
            continue
        assert not params.accepts_temperature, prefix
