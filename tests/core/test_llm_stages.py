"""단계별 모델 티어 해석 검증 (#106).

고정하려는 계약은 네 가지다.

1. 모든 `LLMStage` 가 티어 배치를 갖는다(빠진 단계는 조용히 전역 모델로 새지 않는다).
2. 티어 설정이 있으면 그 모델을, 비었거나 공백뿐이면 전역 `{PROVIDER}_MODEL` 을 쓴다.
3. 티어와 전역이 모두 비면 기존 `{PROVIDER}_MODEL 이 설정되지 않았습니다` 로 실패한다.
4. 티어가 다른 Agent 는 다른 모델을, 같은 티어끼리는 같은 provider 인스턴스를 쓴다.
"""

import pytest

from app.agents.parsing import default_llm
from app.core import llm as llm_module
from app.core import llm_stages
from app.core.llm_stages import (
    LLMStage,
    LLMTier,
    global_model,
    model_for_stage,
    model_for_tier,
    overridden_tier_models,
    resolved_tier_models,
    tier_of,
    tier_setting_name,
)


@pytest.fixture
def stage_settings(monkeypatch: pytest.MonkeyPatch):
    """provider·전역 모델·티어 모델을 한 번에 세우는 헬퍼.

    `llm_stages` 와 `llm` 두 모듈이 각자 import 한 `settings` 를 보므로 양쪽을 다 세운다.
    provider 인스턴스는 `lru_cache` 라 테스트마다 비운다.
    """

    def _apply(
        *,
        provider: str = "openai",
        provider_model: str = "global-model",
        fast: str = "",
        quality: str = "",
    ) -> None:
        for module in (llm_stages, llm_module):
            monkeypatch.setattr(module.settings, "llm_provider", provider, raising=False)
            monkeypatch.setattr(
                module.settings, f"{provider}_model", provider_model, raising=False
            )
            monkeypatch.setattr(module.settings, "llm_model_fast", fast, raising=False)
            monkeypatch.setattr(
                module.settings, "llm_model_quality", quality, raising=False
            )
        llm_module.get_provider.cache_clear()

    llm_module.get_provider.cache_clear()
    yield _apply
    llm_module.get_provider.cache_clear()


# --- 1. 배치가 빠진 단계가 없다 -------------------------------------------------


def test_every_stage_has_a_tier() -> None:
    for stage in LLMStage:
        assert isinstance(tier_of(stage), LLMTier)


def test_every_tier_has_a_settings_field() -> None:
    from app.core.config import Settings

    for tier in LLMTier:
        assert tier_setting_name(tier) in Settings.model_fields


# --- 2. 해석 순서: 티어 설정 > 전역 모델 -----------------------------------------


def test_tier_model_wins_when_configured(stage_settings) -> None:
    stage_settings(fast="cheap-model", quality="good-model")

    assert model_for_stage(LLMStage.LOCATION) == "cheap-model"
    assert model_for_stage(LLMStage.TIMELINE) == "good-model"


def test_unset_tier_falls_back_to_global_model(stage_settings) -> None:
    """티어를 비우면 `None` 이 나와야 한다 — 그래야 provider 싱글턴을 재사용한다."""

    stage_settings(fast="", quality="good-model")

    assert model_for_stage(LLMStage.LOCATION) is None
    assert model_for_tier(LLMTier.FAST) is None
    assert model_for_stage(LLMStage.TIMELINE) == "good-model"


def test_whitespace_only_tier_model_is_treated_as_unset(stage_settings) -> None:
    stage_settings(fast="   ")

    assert model_for_tier(LLMTier.FAST) is None


def test_no_stage_means_global_model() -> None:
    assert model_for_stage(None) is None


def test_resolved_tier_models_fills_in_the_global_model(stage_settings) -> None:
    stage_settings(provider_model="global-model", fast="", quality="good-model")

    assert resolved_tier_models() == {
        LLMTier.FAST: "global-model",
        LLMTier.QUALITY: "good-model",
    }


def test_global_model_follows_the_selected_provider(stage_settings) -> None:
    stage_settings(provider="gemini", provider_model="gemini-model")

    assert global_model() == "gemini-model"


# --- 운영 로그에 덧붙는 필드 -------------------------------------------------------


def test_no_tier_override_means_no_extra_log_fields(stage_settings) -> None:
    """티어를 설정하지 않으면 Main Agent 로그 줄이 예전과 그대로여야 한다."""

    stage_settings(provider_model="global-model", fast="", quality="")

    assert overridden_tier_models() == {}


def test_tier_equal_to_global_is_not_reported_as_an_override(stage_settings) -> None:
    stage_settings(provider_model="global-model", fast="global-model", quality="")

    assert overridden_tier_models() == {}


def test_only_differing_tiers_are_reported(stage_settings) -> None:
    stage_settings(provider_model="global-model", fast="", quality="good-model")

    assert overridden_tier_models() == {LLMTier.QUALITY: "good-model"}


# --- 3. 티어와 전역이 모두 비면 기존 오류가 그대로 난다 ----------------------------


def test_missing_model_everywhere_keeps_the_existing_error(stage_settings) -> None:
    stage_settings(provider_model="", fast="", quality="")

    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        default_llm(LLMStage.LOCATION)


# --- 4. Agent 별 모델 선택과 인스턴스 공유 ----------------------------------------


def test_agents_in_different_tiers_get_different_models(
    stage_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_settings(fast="cheap-model", quality="good-model")
    monkeypatch.setattr(llm_module.OpenAIProvider, "_build_client", lambda self: object())
    monkeypatch.setattr(llm_module, "resolve_secret", lambda name: "test-key")

    assert default_llm(LLMStage.LOCATION).provider.model == "cheap-model"
    assert default_llm(LLMStage.PHOTO_DESCRIBE).provider.model == "cheap-model"
    assert default_llm(LLMStage.TIMELINE).provider.model == "good-model"
    assert default_llm(LLMStage.USER_MEMORY).provider.model == "good-model"


def test_same_tier_stages_share_one_provider_instance(
    stage_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """티어가 둘이면 provider SDK 클라이언트도 둘까지만 생겨야 한다."""

    stage_settings(fast="cheap-model", quality="good-model")
    monkeypatch.setattr(llm_module.OpenAIProvider, "_build_client", lambda self: object())
    monkeypatch.setattr(llm_module, "resolve_secret", lambda name: "test-key")

    location = default_llm(LLMStage.LOCATION).provider
    notification = default_llm(LLMStage.NOTIFICATION).provider
    timeline = default_llm(LLMStage.TIMELINE).provider
    repair = default_llm(LLMStage.REPAIR).provider

    assert location is notification
    assert timeline is repair
    assert location is not timeline
    assert len({id(p) for p in (location, notification, timeline, repair)}) == 2


def test_unconfigured_tiers_keep_the_single_provider_singleton(
    stage_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """티어 설정이 없으면 지금까지처럼 인스턴스가 하나뿐이어야 한다."""

    stage_settings(provider_model="global-model", fast="", quality="")
    monkeypatch.setattr(llm_module.OpenAIProvider, "_build_client", lambda self: object())
    monkeypatch.setattr(llm_module, "resolve_secret", lambda name: "test-key")

    providers = {
        id(default_llm(stage).provider) for stage in LLMStage
    }

    assert len(providers) == 1
    assert default_llm(LLMStage.TIMELINE).provider.model == "global-model"
