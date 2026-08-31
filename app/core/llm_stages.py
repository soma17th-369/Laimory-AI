"""LLM 호출 단계와 단계별 모델 티어 (#106).

LLM 을 부르는 지점마다 필요한 추론 수준과 입력 크기가 다르다. provider 는 전역
하나(`LLM_PROVIDER`)로 두되 **모델만** 티어로 갈라 쉬운 단계는 싼 모델, 어려운 단계는
좋은 모델을 쓸 수 있게 한다.

이 모듈이 세 가지를 소유한다.

- :class:`LLMTier` — 모델 티어. 이름은 **모델 자체의 성질**만 가리킨다. 어느 단계를 어느
  티어에 둘지는 고정이 아니라 언제든 바꾸는 값이라, 티어 이름에 용도(`EXTRACT`/`REASON`
  같은)를 담으면 배치를 바꾸는 순간 이름이 거짓이 된다.
- :class:`LLMStage` — LLM 호출 지점. :class:`~app.core.execution_context.ExecutionStage`
  와 **다른 값이다.** 저쪽은 `EVENT_AGENT` 하나로 Event Agent 5개를 덮고 사진 설명 단계가
  없어 모델 설정 단위로 쓸 수 없다. 저쪽은 로그 상관키와 Langfuse 계층을 정하고, 이쪽은
  모델 해석에만 쓴다.
- ``_STAGE_TIERS`` — 단계→티어 배치. 모든 단계가 여기 있어야 한다(테스트가 강제한다).

모델 해석 순서는 **`{PROVIDER}_MODEL_{TIER}` → `{PROVIDER}_MODEL`** 이다. 티어는
**provider 별로 갈린다** — provider 마다 쓸 수 있는 모델이 다르기 때문이다(`gpt-5.4-mini`
는 OpenAI API 에만 있고 Bedrock 에는 없다). 공용 티어 하나로 두면 provider 를 바꾸는
순간 그 provider 에 없는 모델 id 를 부르게 된다.

티어 설정이 비어 있으면 그 provider 의 `{PROVIDER}_MODEL` 을 쓰므로, 티어를 하나도
넣지 않으면 동작이 예전과 달라지지 않는다.

관측에는 이 값을 싣지 않는다. Langfuse generation 은 이미 `model` 로 **실제 호출한
모델**을 남기고(`app/core/llm.py`) 단계는 generation 이름이 가르므로 더할 것이 없다.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.config import settings


class LLMTier(StrEnum):
    """모델 티어. 값 문자열이 곧 설정 이름의 접미사다(`{PROVIDER}_MODEL_{VALUE}`)."""

    #: 빠르고 싼 쪽.
    FAST = "fast"
    #: 품질을 우선하는 쪽.
    QUALITY = "quality"


class LLMStage(StrEnum):
    """LLM 을 부르는 지점."""

    LOCATION = "location"
    CALENDAR = "calendar"
    PHOTO = "photo"
    #: 사진 설명 생성. vision 호출과 그 fallback(메타데이터 텍스트 호출)을 함께 덮는다 —
    #: 둘이 같은 "사진 설명" 결과를 만들고 이미 같은 LLM 을 공유한다.
    #: **이 단계만 이미지 입력을 쓴다.** 이 단계가 속한 티어의 모델은 vision 을
    #: 지원해야 한다.
    PHOTO_DESCRIBE = "photo_describe"
    SLEEP_ACTIVITY = "sleep_activity"
    NOTIFICATION = "notification"
    TIMELINE = "timeline"
    REPAIR = "repair"
    QUESTION = "question"
    USER_MEMORY = "user_memory"


#: 단계→티어 배치. **고정값이 아니라 운영하며 바꾸는 값이다.**
#:
#: 지금 기준은 "사용자가 읽을 문장을 직접 쓰거나 하루 전체를 조율하는가" 다. Event 계열은
#: 자기 source 에서 사실을 뽑아 내부로만 넘기고, Timeline·Repair·Question 은 사용자가 읽는
#: 일기와 질문을 쓴다(#61, #66). User Memory 는 사용자에게 보이지 않지만 사람에 대한 해석을
#: 다시 쓰는 일이라 같은 쪽에 둔다(#64).
_STAGE_TIERS: dict[LLMStage, LLMTier] = {
    LLMStage.LOCATION: LLMTier.FAST,
    LLMStage.CALENDAR: LLMTier.FAST,
    LLMStage.PHOTO: LLMTier.FAST,
    LLMStage.PHOTO_DESCRIBE: LLMTier.FAST,
    LLMStage.SLEEP_ACTIVITY: LLMTier.FAST,
    LLMStage.NOTIFICATION: LLMTier.FAST,
    LLMStage.TIMELINE: LLMTier.QUALITY,
    LLMStage.REPAIR: LLMTier.QUALITY,
    LLMStage.QUESTION: LLMTier.QUALITY,
    LLMStage.USER_MEMORY: LLMTier.QUALITY,
}


def current_provider() -> str:
    """지금 선택된 provider 이름."""

    return str(settings.llm_provider or "").strip().lower()


def tier_setting_name(provider: str, tier: LLMTier) -> str:
    """티어 모델을 담는 `Settings` 필드 이름.

    기존 `{provider}_model` 규칙을 그대로 늘린 `{provider}_model_{tier}` 다
    (`OPENAI_MODEL_FAST` → `openai_model_fast`).
    """

    return f"{provider}_model_{tier.value}"


def tier_of(stage: LLMStage) -> LLMTier:
    """단계가 속한 티어."""

    return _STAGE_TIERS[stage]


def default_model() -> str:
    """현재 provider 의 `{PROVIDER}_MODEL`. 티어 설정이 없을 때 쓰는 기본값이다."""

    return str(getattr(settings, f"{current_provider()}_model", "") or "").strip()


def model_for_tier(tier: LLMTier) -> str | None:
    """현재 provider 에서 그 티어에 지정된 모델. 지정이 없으면 ``None``.

    티어는 provider 별로 갈린다. provider 마다 쓸 수 있는 모델이 다르기 때문이다 —
    공용 티어 하나로 두면 provider 를 바꾸는 순간 그 provider 에 없는 모델 id 를 부른다.
    티어 필드를 두지 않은 provider(gemini)는 언제나 ``None`` 이라 기본값을 쓴다.

    ``None`` 은 "기본 모델을 쓰라"는 뜻이다. 여기서 기본 모델로 바꿔 돌려주지 않는 이유는
    호출부가 그 차이를 알아야 하기 때문이다 — `LLMClient()` 는 model 을 주지 않아야
    provider 싱글턴을 재사용한다.
    """

    value = getattr(settings, tier_setting_name(current_provider(), tier), "")
    return str(value).strip() or None


def model_for_stage(stage: LLMStage | None) -> str | None:
    """단계에 적용할 모델. 지정이 없거나 단계가 없으면 ``None``(기본 모델)."""

    if stage is None:
        return None
    return model_for_tier(tier_of(stage))


def resolved_tier_models() -> dict[LLMTier, str]:
    """티어별로 **실제 적용될** 모델. 티어 설정이 없으면 기본 모델이 들어간다."""

    fallback = default_model()
    return {tier: (model_for_tier(tier) or fallback) for tier in LLMTier}


def overridden_tier_models() -> dict[LLMTier, str]:
    """기본 모델과 **다른** 티어만 돌려준다.

    운영 로그에 "이 실행이 기본 말고 어떤 모델을 걸치고 있나" 를 남기는 데 쓴다.
    티어 설정이 없으면 빈 dict 라 로그 줄이 예전과 그대로다 — 아무것도 설정하지 않은
    배포에서 새 필드가 늘지 않아야 기존 대시보드·필터가 그대로 동작한다.
    """

    fallback = default_model()
    return {
        tier: model
        for tier, model in resolved_tier_models().items()
        if model != fallback
    }


__all__ = [
    "LLMStage",
    "LLMTier",
    "current_provider",
    "default_model",
    "model_for_stage",
    "model_for_tier",
    "overridden_tier_models",
    "resolved_tier_models",
    "tier_of",
    "tier_setting_name",
]
