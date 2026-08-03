"""프롬프트 버전에 따른 Agent 실행 구조 계약 (#56).

Location 과 Sleep/Activity 는 v1 에서 **infer → review** 2단계였다. v2 부터는 review 를
두지 않고 `infer` 한 번으로 끝낸다. 분기 기준은 `settings.prompt_version` 이며, 이는
`PROMPT_VERSION=v1` 롤백이 프롬프트뿐 아니라 **실행 구조까지** 되돌리게 하기 위한 것이다.

이 계약이 조용히 깨지면 v1 으로 되돌려도 v1 동작이 아니게 되므로 호출 수로 못 박는다.
"""

import importlib

import pytest

from app.core import config
from tests.fixtures.fake_llm import FakeLLM, result_json
from tests.fixtures.requests import make_request, sleep_item, stay_item

_AGENT_MODULES = {
    "location": "app.agents.events.location.agent",
    "sleep_activity": "app.agents.events.sleep_activity.agent",
}


def _reload_agents(monkeypatch: pytest.MonkeyPatch, version: str) -> dict:
    """`PROMPT_VERSION` 을 바꿔 두 agent 모듈을 다시 import 한다.

    `_USE_REVIEW` 는 모듈 로드 시점에 정해지므로 reload 없이는 버전을 바꿀 수 없다.
    """

    monkeypatch.setenv("PROMPT_VERSION", version)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "settings", config.get_settings())

    reloaded = {}
    for name, module_path in _AGENT_MODULES.items():
        module = importlib.import_module(module_path)
        reloaded[name] = importlib.reload(module)
    return reloaded


@pytest.fixture(autouse=True)
def _restore_default_prompt_version():
    """테스트가 바꾼 모듈 상태를 기본 버전으로 되돌린다."""

    yield
    config.get_settings.cache_clear()
    config.settings = config.get_settings()
    for module_path in _AGENT_MODULES.values():
        importlib.reload(importlib.import_module(module_path))


def _location_request():
    return make_request(stays=[stay_item("stay-1", place="집")], movements=[])


def _sleep_request():
    return make_request(
        healths=[sleep_item("sleep-1", "2026-06-20T00:20:00", "2026-06-20T07:10:00", 410)]
    )


@pytest.mark.parametrize(
    ("agent_name", "attr", "build_request"),
    [
        ("location", "LocationEventAgent", _location_request),
        ("sleep_activity", "SleepActivityEventAgent", _sleep_request),
    ],
)
def test_v1_runs_infer_then_review(
    monkeypatch: pytest.MonkeyPatch, agent_name, attr, build_request
) -> None:
    modules = _reload_agents(monkeypatch, "v1")
    module = modules[agent_name]
    assert module._REVIEW_PROMPT is not None

    # 1) infer 의 자유 텍스트 초안, 2) review 의 구조화 응답.
    llm = FakeLLM(["초안 텍스트", result_json()])
    getattr(module, attr)(llm=llm).generate(build_request())

    assert len(llm.calls) == 2, "v1 은 infer → review 2회 호출이어야 합니다."
    assert "{{DRAFT}}" not in llm.calls[1].prompt
    assert "초안 텍스트" in llm.calls[1].prompt


@pytest.mark.parametrize(
    ("agent_name", "attr", "build_request"),
    [
        ("location", "LocationEventAgent", _location_request),
        ("sleep_activity", "SleepActivityEventAgent", _sleep_request),
    ],
)
def test_v2_runs_single_structured_call(
    monkeypatch: pytest.MonkeyPatch, agent_name, attr, build_request
) -> None:
    modules = _reload_agents(monkeypatch, "v2")
    module = modules[agent_name]
    assert module._REVIEW_PROMPT is None, "v2 는 review 프롬프트를 읽지 않습니다."

    llm = FakeLLM([result_json()])
    getattr(module, attr)(llm=llm).generate(build_request())

    assert len(llm.calls) == 1, "v2 는 단일 structured 호출이어야 합니다."
