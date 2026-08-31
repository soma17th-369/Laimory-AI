"""provider 별 단계 모델 티어 해석 검증 (#106).

고정하려는 계약은 다섯 가지다.

1. 모든 `LLMStage` 가 티어 배치를 갖는다(빠진 단계는 조용히 기본 모델로 새지 않는다).
2. 티어 설정이 있으면 그 모델을, 비었거나 공백뿐이면 `{PROVIDER}_MODEL` 을 쓴다.
3. **티어는 provider 별로 갈린다.** provider 를 바꾸면 그 provider 의 티어만 본다.
4. 티어와 기본 모델이 모두 비면 기존 `{PROVIDER}_MODEL 이 설정되지 않았습니다` 로 실패한다.
5. 티어가 다른 Agent 는 다른 모델을, 같은 티어끼리는 같은 provider 인스턴스를 쓴다.
"""

import pytest

from app.agents.parsing import default_llm
from app.core import llm as llm_module
from app.core import llm_stages
from app.core.llm_stages import (
    LLMStage,
    LLMTier,
    current_provider,
    default_model,
    model_for_stage,
    model_for_tier,
    overridden_tier_models,
    resolved_tier_models,
    tier_of,
    tier_setting_name,
)

#: 티어 필드를 선언한 provider 들. gemini 는 일부러 빠져 있다(테스트 6 참고).
TIERED_PROVIDERS = ("bedrock", "openai")


@pytest.fixture
def stage_settings(monkeypatch: pytest.MonkeyPatch):
    """provider·기본 모델·provider 별 티어 모델을 한 번에 세우는 헬퍼.

    `llm_stages` 와 `llm` 두 모듈이 각자 import 한 `settings` 를 보므로 양쪽을 다 세운다.
    provider 인스턴스는 `lru_cache` 라 테스트마다 비운다.
    """

    def _apply(
        *,
        provider: str = "openai",
        default: str = "default-model",
        fast: str = "",
        quality: str = "",
        other_provider_fast: str = "other-fast",
    ) -> None:
        for module in (llm_stages, llm_module):
            target = module.settings
            monkeypatch.setattr(target, "llm_provider", provider, raising=False)
            monkeypatch.setattr(target, f"{provider}_model", default, raising=False)
            monkeypatch.setattr(target, f"{provider}_model_fast", fast, raising=False)
            monkeypatch.setattr(
                target, f"{provider}_model_quality", quality, raising=False
            )
            # 다른 provider 의 티어가 새어 들어오지 않는지 보려고 값을 심어 둔다.
            for name in TIERED_PROVIDERS:
                if name != provider:
                    monkeypatch.setattr(
                        target, f"{name}_model_fast", other_provider_fast, raising=False
                    )
        llm_module.get_provider.cache_clear()

    llm_module.get_provider.cache_clear()
    yield _apply
    llm_module.get_provider.cache_clear()


# --- 1. 배치가 빠진 단계가 없다 -------------------------------------------------


def test_every_stage_has_a_tier() -> None:
    for stage in LLMStage:
        assert isinstance(tier_of(stage), LLMTier)


def test_tiered_providers_declare_every_tier_field() -> None:
    from app.core.config import Settings

    for provider in TIERED_PROVIDERS:
        for tier in LLMTier:
            assert tier_setting_name(provider, tier) in Settings.model_fields


def test_tier_setting_name_follows_the_provider_model_convention() -> None:
    assert tier_setting_name("openai", LLMTier.FAST) == "openai_model_fast"
    assert tier_setting_name("bedrock", LLMTier.QUALITY) == "bedrock_model_quality"


# --- 2. 해석 순서: {PROVIDER}_MODEL_{TIER} > {PROVIDER}_MODEL --------------------


def test_tier_model_wins_when_configured(stage_settings) -> None:
    stage_settings(fast="cheap-model", quality="good-model")

    assert model_for_stage(LLMStage.LOCATION) == "cheap-model"
    assert model_for_stage(LLMStage.TIMELINE) == "good-model"


def test_unset_tier_falls_back_to_the_provider_model(stage_settings) -> None:
    """티어를 비우면 `None` 이 나와야 한다 — 그래야 provider 싱글턴을 재사용한다."""

    stage_settings(fast="", quality="good-model")

    assert model_for_stage(LLMStage.LOCATION) is None
    assert model_for_tier(LLMTier.FAST) is None
    assert model_for_stage(LLMStage.TIMELINE) == "good-model"


def test_whitespace_only_tier_model_is_treated_as_unset(stage_settings) -> None:
    stage_settings(fast="   ")

    assert model_for_tier(LLMTier.FAST) is None


def test_no_stage_means_the_default_model() -> None:
    assert model_for_stage(None) is None


def test_resolved_tier_models_fills_in_the_default_model(stage_settings) -> None:
    stage_settings(default="default-model", fast="", quality="good-model")

    assert resolved_tier_models() == {
        LLMTier.FAST: "default-model",
        LLMTier.QUALITY: "good-model",
    }


# --- 3. 티어는 provider 별로 갈린다 ----------------------------------------------


def test_current_provider_is_normalized(stage_settings) -> None:
    stage_settings(provider="openai")
    assert current_provider() == "openai"


def test_default_model_follows_the_selected_provider(stage_settings) -> None:
    stage_settings(provider="bedrock", default="bedrock-default")

    assert default_model() == "bedrock-default"


def test_another_providers_tier_does_not_leak(stage_settings) -> None:
    """openai 를 쓰는데 bedrock 티어 값이 섞여 들어오면 안 된다.

    provider 마다 쓸 수 있는 모델 id 가 달라, 새면 그 provider 에 없는 모델을 부른다.
    """

    stage_settings(
        provider="openai", default="openai-default", fast="", other_provider_fast="bedrock-fast"
    )

    assert model_for_tier(LLMTier.FAST) is None
    assert resolved_tier_models()[LLMTier.FAST] == "openai-default"


def test_each_provider_reads_its_own_tier(stage_settings) -> None:
    stage_settings(provider="bedrock", default="bedrock-default", fast="bedrock-fast")
    assert model_for_stage(LLMStage.LOCATION) == "bedrock-fast"

    stage_settings(provider="openai", default="openai-default", fast="openai-fast")
    assert model_for_stage(LLMStage.LOCATION) == "openai-fast"


def test_provider_without_tier_fields_always_uses_its_model(
    stage_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gemini 에는 티어 필드가 없다. 언제나 `GEMINI_MODEL` 하나를 쓴다."""

    for module in (llm_stages, llm_module):
        monkeypatch.setattr(module.settings, "llm_provider", "gemini", raising=False)
        monkeypatch.setattr(module.settings, "gemini_model", "gemini-model", raising=False)

    assert model_for_tier(LLMTier.FAST) is None
    assert model_for_tier(LLMTier.QUALITY) is None
    assert resolved_tier_models() == {
        LLMTier.FAST: "gemini-model",
        LLMTier.QUALITY: "gemini-model",
    }


# --- 운영 로그에 덧붙는 필드 -------------------------------------------------------


def test_no_tier_override_means_no_extra_log_fields(stage_settings) -> None:
    """티어를 설정하지 않으면 Main Agent 로그 줄이 예전과 그대로여야 한다."""

    stage_settings(default="default-model", fast="", quality="")

    assert overridden_tier_models() == {}


def test_tier_equal_to_default_is_not_reported_as_an_override(stage_settings) -> None:
    stage_settings(default="default-model", fast="default-model", quality="")

    assert overridden_tier_models() == {}


def test_only_differing_tiers_are_reported(stage_settings) -> None:
    stage_settings(default="default-model", fast="", quality="good-model")

    assert overridden_tier_models() == {LLMTier.QUALITY: "good-model"}


# --- 4. 티어와 기본 모델이 모두 비면 기존 오류가 그대로 난다 ------------------------


def test_missing_model_everywhere_keeps_the_existing_error(stage_settings) -> None:
    stage_settings(default="", fast="", quality="")

    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        default_llm(LLMStage.LOCATION)


# --- 5. Agent 별 모델 선택과 인스턴스 공유 ----------------------------------------


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 SDK 를 만들지 않고 provider 인스턴스만 세운다."""

    monkeypatch.setattr(llm_module.OpenAIProvider, "_build_client", lambda self: object())
    monkeypatch.setattr(llm_module, "resolve_secret", lambda name: "test-key")


def test_agents_in_different_tiers_get_different_models(
    stage_settings, fake_openai
) -> None:
    stage_settings(fast="cheap-model", quality="good-model")

    assert default_llm(LLMStage.LOCATION).provider.model == "cheap-model"
    assert default_llm(LLMStage.PHOTO_DESCRIBE).provider.model == "cheap-model"
    assert default_llm(LLMStage.TIMELINE).provider.model == "good-model"
    assert default_llm(LLMStage.USER_MEMORY).provider.model == "good-model"


def test_same_tier_stages_share_one_provider_instance(
    stage_settings, fake_openai
) -> None:
    """티어가 둘이면 provider SDK 클라이언트도 둘까지만 생겨야 한다."""

    stage_settings(fast="cheap-model", quality="good-model")

    location = default_llm(LLMStage.LOCATION).provider
    notification = default_llm(LLMStage.NOTIFICATION).provider
    timeline = default_llm(LLMStage.TIMELINE).provider
    repair = default_llm(LLMStage.REPAIR).provider

    assert location is notification
    assert timeline is repair
    assert location is not timeline
    assert len({id(p) for p in (location, notification, timeline, repair)}) == 2


def test_unconfigured_tiers_keep_the_single_provider_singleton(
    stage_settings, fake_openai
) -> None:
    """티어 설정이 없으면 지금까지처럼 인스턴스가 하나뿐이어야 한다."""

    stage_settings(default="default-model", fast="", quality="")

    providers = {id(default_llm(stage).provider) for stage in LLMStage}

    assert len(providers) == 1
    assert default_llm(LLMStage.TIMELINE).provider.model == "default-model"
