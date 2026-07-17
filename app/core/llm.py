"""LLM 클라이언트.

여러 LLM provider(OpenAI, Gemini 등)를 공통 인터페이스(`LLMProvider`)로 감싼다.
사용할 provider 는 `settings.llm_provider` 로 결정하며, `LLMClient` 를 통해
provider 종류와 무관하게 동일한 방식으로 호출한다.

새 provider 추가 방법:
1. `app/core/config.py` 에 `{name}_api_key`, `{name}_model` 필드를 추가한다.
2. 아래에 `LLMProvider` 를 상속한 클래스를 만들고 `@register_provider` 로 등록한다.
   (`name` 클래스 변수와 `_build_client`, `complete` 만 구현하면 된다.)
"""

import base64
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.observability import (
    ObservationEventType,
    ObservationStage,
    emit_observation,
)

logger = get_logger(__name__)

# provider 이름 -> provider 클래스 레지스트리
_PROVIDERS: dict[str, type["LLMProvider"]] = {}


@dataclass(frozen=True)
class ImageInput:
    """vision 호출에 넘길 이미지 한 장 (raw bytes + MIME 타입)."""

    data: bytes
    mime_type: str = "image/jpeg"


@dataclass(frozen=True)
class TokenUsage:
    """Provider가 보고한 요청별 토큰 사용량의 공통 표현."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    tool_tokens: int | None = None
    provider_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMCompletion:
    """텍스트와 같은 응답에서 추출한 사용량을 함께 운반한다."""

    text: str
    usage: TokenUsage | None = None


def _dump_model(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return dict(value)
    return None


def _openai_usage(response: Any) -> TokenUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    details = {
        key: value
        for key, value in {
            "promptTokensDetails": _dump_model(prompt_details),
            "completionTokensDetails": _dump_model(completion_details),
        }.items()
        if value
    }
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        cached_tokens=getattr(prompt_details, "cached_tokens", None),
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None),
        provider_details=details or None,
    )


def _gemini_usage(response: Any) -> TokenUsage | None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None

    values = {
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
        "cached_tokens": getattr(usage, "cached_content_token_count", None),
        "reasoning_tokens": getattr(usage, "thoughts_token_count", None),
        "tool_tokens": getattr(usage, "tool_use_prompt_token_count", None),
    }
    if not any(value is not None for value in values.values()):
        return None
    return TokenUsage(
        **values,
        provider_details=_dump_model(usage),
    )


def _as_completion(result: LLMCompletion | str) -> LLMCompletion:
    """기존 문자열 반환 provider도 안전하게 수용한다."""

    if isinstance(result, LLMCompletion):
        return result
    return LLMCompletion(text=result)


def register_provider(cls: type["LLMProvider"]) -> type["LLMProvider"]:
    """provider 클래스를 레지스트리에 등록하는 데코레이터."""

    _PROVIDERS[cls.name] = cls
    return cls


class LLMProvider(ABC):
    """모든 LLM provider 가 구현하는 공통 인터페이스.

    자격 증명(`api_key`)과 모델(`model`)은 `settings` 에서
    `{name}_api_key` / `{name}_model` 규칙으로 읽는다.
    """

    # 하위 클래스에서 반드시 지정한다. (예: "openai", "gemini")
    name: str = ""

    def __init__(self, model: str | None = None) -> None:
        self.api_key: str = getattr(settings, f"{self.name}_api_key", "")
        self.model: str = model or getattr(settings, f"{self.name}_model", "")

        if not self.api_key:
            raise ValueError(
                f"{self.name.upper()}_API_KEY 가 설정되지 않았습니다. .env 를 확인하세요."
            )
        if not self.model:
            raise ValueError(
                f"{self.name.upper()}_MODEL 이 설정되지 않았습니다. .env 를 확인하세요."
            )

        self.client = self._build_client()

    @abstractmethod
    def _build_client(self):
        """provider SDK 클라이언트를 생성해 반환한다."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMCompletion:
        """단일 프롬프트의 텍스트와 provider 사용량을 반환한다."""

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMCompletion:
        """프롬프트 + 이미지(vision) 입력에 대한 응답 텍스트를 반환한다.

        vision 을 지원하는 provider 만 override 한다. 기본은 미지원 예외.
        """

        raise NotImplementedError(
            f"{self.name} provider 는 이미지(vision) 입력을 지원하지 않습니다."
        )


@register_provider
class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions provider."""

    name = "openai"

    def _build_client(self):
        from openai import OpenAI

        return OpenAI(api_key=self.api_key)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMCompletion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            **kwargs,
        )
        return LLMCompletion(
            text=response.choices[0].message.content or "",
            usage=_openai_usage(response),
        )

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMCompletion:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            b64 = base64.b64encode(image.data).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.mime_type};base64,{b64}"},
                }
            )
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            **kwargs,
        )
        return LLMCompletion(
            text=response.choices[0].message.content or "",
            usage=_openai_usage(response),
        )


@register_provider
class GeminiProvider(LLMProvider):
    """Google Gemini provider (google-genai SDK)."""

    name = "gemini"

    def _build_client(self):
        from google import genai

        return genai.Client(api_key=self.api_key)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMCompletion:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            **kwargs,
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        return LLMCompletion(
            text=response.text or "",
            usage=_gemini_usage(response),
        )

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMCompletion:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            **kwargs,
        )
        parts: list = [prompt]
        for image in images:
            parts.append(
                types.Part.from_bytes(data=image.data, mime_type=image.mime_type)
            )
        response = self.client.models.generate_content(
            model=self.model,
            contents=parts,
            config=config,
        )
        return LLMCompletion(
            text=response.text or "",
            usage=_gemini_usage(response),
        )


def available_providers() -> list[str]:
    """등록된 provider 이름 목록을 반환한다."""

    return sorted(_PROVIDERS)


@lru_cache
def get_provider(name: str | None = None) -> LLMProvider:
    """provider 인스턴스(싱글턴)를 반환한다.

    Args:
        name: provider 이름. 생략하면 `settings.llm_provider` 를 사용한다.
    """

    provider_name = (name or settings.llm_provider).lower()
    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ValueError(
            f"지원하지 않는 LLM provider: '{provider_name}'. "
            f"사용 가능: {available_providers()}"
        )
    return provider_cls()


class LLMClient:
    """설정된 provider 를 사용하는 공통 LLM 클라이언트(facade).

    provider 종류와 무관하게 `complete()` 로 동일하게 호출한다.
    """

    def __init__(self, provider: str | None = None, model: str | None = None) -> None:
        if model is None:
            # 기본 모델 사용 시 provider 싱글턴을 재사용한다.
            self.provider = get_provider(provider)
        else:
            # 모델을 명시하면 해당 모델로 새 provider 인스턴스를 만든다.
            provider_name = (provider or settings.llm_provider).lower()
            provider_cls = _PROVIDERS.get(provider_name)
            if provider_cls is None:
                raise ValueError(
                    f"지원하지 않는 LLM provider: '{provider_name}'. "
                    f"사용 가능: {available_providers()}"
                )
            self.provider = provider_cls(model=model)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """단일 프롬프트에 대한 응답 텍스트를 반환한다.

        Args:
            prompt: 사용자 프롬프트.
            system: 시스템 프롬프트(선택).
            temperature: 샘플링 온도.
            **kwargs: 각 provider SDK 로 그대로 전달되는 추가 인자.
        """

        logger.debug(
            "LLM 호출: provider=%s, model=%s, temperature=%s",
            self.provider.name,
            self.provider.model,
            temperature,
        )
        emit_observation(
            ObservationEventType.PROMPT,
            stage=ObservationStage.LLM,
            provider=self.provider.name,
            model=self.provider.model,
            payload={
                "prompt": prompt,
                "system": system,
                "temperature": temperature,
                "options": kwargs,
            },
        )
        started = time.perf_counter()
        try:
            completion = _as_completion(
                self.provider.complete(
                    prompt,
                    system=system,
                    temperature=temperature,
                    **kwargs,
                )
            )
        except Exception as exc:
            emit_observation(
                ObservationEventType.FAILED,
                stage=ObservationStage.LLM,
                provider=self.provider.name,
                model=self.provider.model,
                duration_ms=(time.perf_counter() - started) * 1000,
                payload={"error": str(exc), "errorType": type(exc).__name__},
            )
            raise

        usage = completion.usage
        response_payload: dict[str, Any] = {"response": completion.text}
        if usage is not None and usage.provider_details:
            response_payload["usageDetails"] = usage.provider_details
        emit_observation(
            ObservationEventType.RESPONSE,
            stage=ObservationStage.LLM,
            provider=self.provider.name,
            model=self.provider.model,
            duration_ms=(time.perf_counter() - started) * 1000,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            cached_tokens=usage.cached_tokens if usage else None,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            tool_tokens=usage.tool_tokens if usage else None,
            payload=response_payload,
        )
        return completion.text

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """프롬프트 + 이미지(vision) 입력에 대한 응답 텍스트를 반환한다."""

        logger.debug(
            "LLM vision 호출: provider=%s, model=%s, images=%d",
            self.provider.name,
            self.provider.model,
            len(images),
        )
        emit_observation(
            ObservationEventType.PROMPT,
            stage=ObservationStage.LLM,
            provider=self.provider.name,
            model=self.provider.model,
            payload={
                "prompt": prompt,
                "system": system,
                "temperature": temperature,
                "options": kwargs,
                "images": [
                    {"mimeType": image.mime_type, "byteLength": len(image.data)}
                    for image in images
                ],
            },
        )
        started = time.perf_counter()
        try:
            completion = _as_completion(
                self.provider.complete_with_images(
                    prompt,
                    images,
                    system=system,
                    temperature=temperature,
                    **kwargs,
                )
            )
        except Exception as exc:
            emit_observation(
                ObservationEventType.FAILED,
                stage=ObservationStage.LLM,
                provider=self.provider.name,
                model=self.provider.model,
                duration_ms=(time.perf_counter() - started) * 1000,
                payload={"error": str(exc), "errorType": type(exc).__name__},
            )
            raise

        usage = completion.usage
        response_payload: dict[str, Any] = {"response": completion.text}
        if usage is not None and usage.provider_details:
            response_payload["usageDetails"] = usage.provider_details
        emit_observation(
            ObservationEventType.RESPONSE,
            stage=ObservationStage.LLM,
            provider=self.provider.name,
            model=self.provider.model,
            duration_ms=(time.perf_counter() - started) * 1000,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            cached_tokens=usage.cached_tokens if usage else None,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            tool_tokens=usage.tool_tokens if usage else None,
            payload=response_payload,
        )
        return completion.text
