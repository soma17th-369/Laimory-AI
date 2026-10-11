"""프롬프트 버전에 따른 Agent 실행 구조 계약 (#56, #121).

Location 과 Sleep/Activity 는 v1 에서 **infer → review** 2단계였다. v2 부터는 review 를
두지 않고 `infer` 한 번으로 끝낸다. 분기 기준은 `settings.prompt_version` 이며, 이는
`PROMPT_VERSION=v1` 롤백이 프롬프트뿐 아니라 **실행 구조까지** 되돌리게 하기 위한 것이다.

이 계약이 조용히 깨지면 v1 으로 되돌려도 v1 동작이 아니게 되므로 호출 수로 못 박는다.

User Memory 는 실행 구조가 아니라 **갱신 요청에 붙는 지시**가 버전으로 갈린다(#121).
v1·v2 는 `memo` 없는 날 성향 필드를 그대로 두라고 알리고, v3 는 알리지 않는다.
"""

import importlib
from pathlib import Path

import pytest

from app.core import config
from app.schemas.user_memory_update import DailyTimeline
from app.services.user_memory_limits import build_daily_timeline_digest
from tests.fixtures.fake_llm import FakeLLM, result_json
from tests.fixtures.requests import make_request, sleep_item, stay_item
from tests.fixtures.user_memory import (
    change,
    changes_json,
    daily_timeline,
    daily_timeline_event,
    memory_json,
)

_AGENT_MODULES = {
    "location": "app.agents.events.location.agent",
    "sleep_activity": "app.agents.events.sleep_activity.agent",
    "photo_describer": "app.agents.events.photo.describer",
    "user_memory": "app.agents.user_memory.user_memory_agent",
}


def _reload_agents(monkeypatch: pytest.MonkeyPatch, version: str) -> dict:
    """`PROMPT_VERSION` 을 바꿔 agent 모듈을 다시 import 한다.

    `_USE_REVIEW` 는 모듈 로드 시점에 정해지므로 reload 없이는 버전을 바꿀 수 없다.

    `prompt_loader` 를 **먼저** 다시 읽는 것이 중요하다. `load_prompt` 는
    `from app.core.config import settings` 로 settings 를 자기 이름에 묶어 두므로,
    `config.settings` 만 patch 하면 loader 에는 닿지 않는다. 그러면 agent 모듈은 v1 이라
    믿고 `review.md` 를 요청하는데 loader 는 실제 `PROMPT_VERSION` 을 보고 v2 세트에서
    찾다가 죽는다 — v2 에 `review.md` 가 없는 것은 설계대로이므로(#56) 어긋난 쪽은
    하네스다. 실행 환경이 `PROMPT_VERSION=v2` 일 때만 드러났다.
    """

    monkeypatch.setenv("PROMPT_VERSION", version)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "settings", config.get_settings())

    return _reload_prompt_modules()


def _reload_prompt_modules() -> dict:
    """`prompt_loader` → agent 순서로 다시 읽는다. 순서를 바꾸면 위 docstring 의 문제가 난다."""

    importlib.reload(importlib.import_module("app.agents.prompt_loader"))

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
    # 되돌릴 때도 `prompt_loader` 를 함께 읽어야 한다. 안 그러면 직전 테스트가 남긴
    # 버전을 loader 가 계속 들고 있어, agent 는 기본 버전인데 loader 는 아니게 된다.
    _reload_prompt_modules()


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


@pytest.mark.parametrize("version", ["v2", "v3"])
@pytest.mark.parametrize(
    ("agent_name", "attr", "build_request"),
    [
        ("location", "LocationEventAgent", _location_request),
        ("sleep_activity", "SleepActivityEventAgent", _sleep_request),
    ],
)
def test_single_call_versions_run_one_structured_call(
    monkeypatch: pytest.MonkeyPatch, agent_name, attr, build_request, version
) -> None:
    modules = _reload_agents(monkeypatch, version)
    module = modules[agent_name]
    assert module._REVIEW_PROMPT is None, f"{version} 는 review 프롬프트를 읽지 않습니다."

    llm = FakeLLM([result_json()])
    getattr(module, attr)(llm=llm).generate(build_request())

    assert len(llm.calls) == 1, f"{version} 는 단일 structured 호출이어야 합니다."


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_photo_fallback_is_prompt_based_in_every_version(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """사진 fallback 은 버전을 가리지 않고 `describe_prompt.md` 다(#59).

    한때 v2 만 코드 기반 `MetadataPhotoDescriber` 였다(#56 §12). 그런데 vision 경로가
    설정 때문에 아예 돌지 않아, "이미지를 못 구했을 때" 가 아니라 **모든** 사진이 그
    고정 문장을 받고 있었다. vision 을 기본으로 되돌리면서 fallback 도 통일했다.
    """

    describer = _reload_agents(monkeypatch, version)["photo_describer"]

    assert describer._DESCRIBE_PROMPT is not None
    assert not hasattr(describer, "_USE_LLM_METADATA_DESCRIBER")
    fallback = describer.VisionPhotoDescriber(llm=FakeLLM([result_json()])).fallback
    assert isinstance(fallback, describer.LLMPhotoDescriber)


def _digest_without_memo():
    payload = daily_timeline(events=[daily_timeline_event(memo=None)])
    return build_daily_timeline_digest([DailyTimeline.model_validate(payload)])


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_memo_only_versions_tell_the_model_a_day_has_no_memo(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """v1·v2 는 성향 근거가 `memo` 뿐이다(#64). v2 로 되돌리면 이 지시도 돌아와야 한다."""

    module = _reload_agents(monkeypatch, version)["user_memory"]

    prompt = module.build_update_prompt(None, _digest_without_memo())

    assert module._MEMO_ONLY_TRAITS is True
    assert "[근거 없음]" in prompt


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_legacy_versions_take_the_whole_document_from_the_model(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """v1·v2 프롬프트는 문서 전체를 출력하라고 적혀 있다. 모델이 낸 문서가 곧 결과다."""

    module = _reload_agents(monkeypatch, version)["user_memory"]
    existing = module.UserMemory(relationships="김민수: 같은 팀 동료.")
    llm = FakeLLM([memory_json(basicProfile="30대 개발자입니다.")])

    memory = module.UserMemoryAgent(llm=llm).generate(existing, _digest_without_memo())

    assert module._PATCH_OUTPUT is False
    assert memory.basic_profile == "30대 개발자입니다."
    assert memory.relationships == ""


def test_v3_takes_only_the_items_to_change_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v3 는 바꿀 항목만 받아 기존 문서에 끼워 넣는다(#121)."""

    module = _reload_agents(monkeypatch, "v3")["user_memory"]
    existing = module.UserMemory(relationships="김민수: 같은 팀 동료.")
    llm = FakeLLM([changes_json(change("basicProfile", "추가", "30대 개발자입니다."))])

    memory = module.UserMemoryAgent(llm=llm).generate(existing, _digest_without_memo())

    assert module._PATCH_OUTPUT is True
    assert memory.basic_profile == "30대 개발자입니다."
    assert memory.relationships == "김민수: 같은 팀 동료."


def test_v3_does_not_tell_the_model_to_leave_traits_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v3 는 AI 가 쓴 문장도 근거로 읽는다(#121). 그 지시는 시스템 프롬프트와 어긋난다."""

    module = _reload_agents(monkeypatch, "v3")["user_memory"]

    prompt = module.build_update_prompt(None, _digest_without_memo())

    assert module._MEMO_ONLY_TRAITS is False
    assert "[근거 없음]" not in prompt


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_user_memory_agent_loads_the_prompt_of_its_version(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """분기값과 시스템 프롬프트가 같은 버전을 봐야 한다.

    둘이 갈리면 v3 프롬프트에 "성향 필드는 그대로" 지시가 붙거나 그 반대가 된다.
    """

    module = _reload_agents(monkeypatch, version)["user_memory"]
    expected = (
        Path(module.__file__).resolve().parent / "prompts" / version / "prompt.md"
    ).read_text(encoding="utf-8")

    assert module._SYSTEM_PROMPT == expected
