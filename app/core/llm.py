"""LLM 클라이언트.

여러 LLM provider(OpenAI, Gemini 등)를 공통 인터페이스(`LLMProvider`)로 감싼다.
사용할 provider 는 `settings.llm_provider` 로 결정하며, `LLMClient` 를 통해
provider 종류와 무관하게 동일한 방식으로 호출한다.

새 provider 추가 방법:
1. `app/core/config.py` 에 `{name}_model`(키로 인증하면 `{name}_api_key` 도) 필드를 추가한다.
2. 아래에 `LLMProvider` 를 상속한 클래스를 만들고 `@register_provider` 로 등록한다.
   (`name` 클래스 변수와 `_build_client`, `complete` 를 구현한다. 키가 아니라 다른
    방식으로 인증하면 `requires_api_key = False`, 이미지 입력을 지원하면
    `supports_vision = True` 로 둔다. Bedrock 이 그 예다.)
"""

import base64
import importlib.metadata
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger
from app.core.observability import (
    ObservationEventType,
    ObservationStage,
    emit_observation,
)
from app.core.structured import ModelT, run_structured, to_strict_schema

logger = get_logger(__name__)

# provider 이름 -> provider 클래스 레지스트리
_PROVIDERS: dict[str, type["LLMProvider"]] = {}


@lru_cache
def _sdk_version(package: str) -> str | None:
    """설치된 provider SDK 패키지 버전(providerVersion)을 반환한다.

    관측 로그에 "이 호출이 어떤 SDK 버전으로 나갔는지" 를 남기기 위한 것으로,
    패키지를 찾지 못하면 ``None`` 이다.
    """

    if not package:
        return None
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


@dataclass(frozen=True)
class ImageInput:
    """vision 호출에 넘길 이미지 한 장 (raw bytes + MIME 타입)."""

    data: bytes
    mime_type: str = "image/jpeg"


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

    # provider 가 `{name}_api_key` 를 요구하는지. Bedrock 처럼 AWS 자격증명 체인으로
    # 인증하는 provider 는 False 로 두어 api_key 검증을 건너뛴다.
    requires_api_key: bool = True

    # 이미지(vision) 입력 지원 여부. 지원하는 provider 는 True 로 명시하고
    # `complete_with_images` 를 override 한다.
    supports_vision: bool = False

    # providerVersion(관측용) 을 읽어올 설치 SDK 패키지 이름. 예: "openai",
    # "google-genai", "boto3". 지정하지 않으면 providerVersion 은 None 이다.
    sdk_package: str = ""

    def __init__(self, model: str | None = None) -> None:
        self.api_key: str = getattr(settings, f"{self.name}_api_key", "")
        self.model: str = model or getattr(settings, f"{self.name}_model", "")

        if self.requires_api_key and not self.api_key:
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
    ) -> str:
        """단일 프롬프트에 대한 응답 텍스트를 반환한다."""

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """프롬프트 + 이미지(vision) 입력에 대한 응답 텍스트를 반환한다.

        vision 을 지원하는 provider 만 override 한다. 기본은 미지원 예외.
        """

        raise NotImplementedError(
            f"{self.name} provider 는 이미지(vision) 입력을 지원하지 않습니다."
        )

    def complete_json(
        self,
        prompt: str,
        schema: type[ModelT] | None = None,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """provider 가 유효 JSON 을 강제해 **텍스트**로 돌려준다(검증은 안 함).

        기본 구현은 호출자의 프롬프트(각 agent 의 `prompt.md` 등)가 이미 출력 형식을
        지시한다고 보고 프롬프트를 건드리지 않는다. provider 네이티브 JSON 강제는
        하위 클래스가 override 한다: OpenAI ``json_object``, Gemini ``response_mime_type``,
        Bedrock tool-use(``schema`` 필요). ``schema`` 는 그것이 필요한 provider 만 쓴다.

        검증까지 필요하면 ``complete_structured`` 를 쓴다. 항목별 관용 파싱처럼 부분
        손상을 살려야 하는 호출자는 이 메서드로 텍스트를 받아 직접 파싱한다(Timeline).
        """

        return self.complete(
            prompt,
            system=system,
            temperature=temperature,
            **kwargs,
        )

    def complete_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_repairs: int = 1,
        **kwargs,
    ) -> ModelT:
        """응답을 ``schema`` 로 강제해 검증된 Pydantic 인스턴스를 반환한다.

        provider 가 형태(JSON·필수·enum)를 잠그고(``complete_json``), 값·교차 규칙은 우리
        Pydantic 이 최종 검증한다. 검증 실패 시 오류를 붙여 ``max_repairs`` 회 재요청하며,
        모두 실패하면 ``StructuredOutputError`` 를 던진다(상위 Agent 가 warning 으로 흡수).
        """

        return run_structured(
            self.complete_json,
            prompt,
            schema,
            system=system,
            temperature=temperature,
            max_repairs=max_repairs,
            **kwargs,
        )

    def _log_usage(
        self, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        """호출에 사용한 provider/model 과 토큰 사용량(입력/출력)을 로그로 남긴다.

        모델 비교는 코드가 하지 않는다. 설정을 바꿔 실행한 뒤 이 로그의 토큰 양을
        읽어 사람이 판단한다(비용 계산도 로그의 토큰으로 외부에서 한다). usage 를
        제공하지 않는 응답이면 토큰은 ``None`` 으로 남는다.
        """

        logger.info(
            "LLM 토큰 사용량: provider=%s, model=%s, inputTokens=%s, outputTokens=%s",
            self.name,
            self.model,
            input_tokens,
            output_tokens,
        )

    def _provider_version(self) -> str | None:
        """이 호출을 낸 provider SDK 패키지 버전(providerVersion)."""

        return _sdk_version(self.sdk_package)

    @staticmethod
    def _usage_detail(response) -> dict[str, int]:
        """provider 응답에서 정규화된 토큰 상세를 뽑는다(있는 값만 담는다).

        키는 input/output/total/cached/reasoning/tool 중 provider 가 실제로 준 것만
        넣는다. 추정하지 않고(예: tokenizer 로 세지 않음) provider 응답값만 쓴다.
        하위 provider 가 override 한다.
        """

        return {}

    def _emit_llm_prompt(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        image_count: int = 0,
        options: dict | None = None,
    ) -> None:
        """LLM 호출 직전 PROMPT 관측 이벤트를 남긴다(컨텍스트 없으면 no-op)."""

        payload: dict = {"prompt": prompt}
        if system is not None:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        if options:
            # Provider 옵션은 SDK 객체가 섞일 수 있어 ES 직렬화 가능한 형태로 고정한다.
            payload["options"] = json.loads(json.dumps(options, default=str))
        if image_count:
            payload["imageCount"] = image_count
        emit_observation(
            ObservationEventType.PROMPT,
            stage=ObservationStage.LLM,
            provider=self.name,
            model=self.model,
            provider_version=self._provider_version(),
            payload=payload,
        )

    def _emit_llm_response(self, started: float, response, text: str) -> None:
        """LLM 응답 후 RESPONSE 관측 이벤트를 남긴다(duration·provider 토큰 포함)."""

        usage = self._usage_detail(response)
        emit_observation(
            ObservationEventType.RESPONSE,
            stage=ObservationStage.LLM,
            provider=self.name,
            model=self.model,
            provider_version=self._provider_version(),
            duration_ms=(perf_counter() - started) * 1000,
            input_tokens=usage.get("input"),
            output_tokens=usage.get("output"),
            total_tokens=usage.get("total"),
            cached_tokens=usage.get("cached"),
            reasoning_tokens=usage.get("reasoning"),
            tool_tokens=usage.get("tool"),
            payload={"response": text},
        )

    def _emit_llm_failure(self, started: float, exc: Exception) -> None:
        """LLM 호출 실패를 로그와 FAILED 관측 이벤트에 같은 코드로 남긴다.

        provider 별 호출 지점이 여럿이지만 실패는 모두 여기로 모인다. 코드 부여를
        한 곳에서만 하려고 그렇게 뒀다.
        """

        report_error(
            logger,
            ErrorCode.LLM_CALL_FAILED,
            "LLM 호출 실패",
            exc=exc,
            context={"provider": self.name, "model": self.model},
            stage=ObservationStage.LLM,
            provider=self.name,
            model=self.model,
            provider_version=self._provider_version(),
            duration_ms=(perf_counter() - started) * 1000,
        )


@register_provider
class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions provider."""

    name = "openai"
    supports_vision = True
    sdk_package = "openai"

    def _build_client(self):
        from openai import OpenAI

        return OpenAI(api_key=self.api_key)

    @staticmethod
    def _usage(response) -> tuple[int | None, int | None]:
        """OpenAI 응답에서 (입력 토큰, 출력 토큰)을 뽑는다."""

        usage = getattr(response, "usage", None)
        return (
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )

    @staticmethod
    def _usage_detail(response) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        detail: dict[str, int] = {}
        for key, attr in (
            ("input", "prompt_tokens"),
            ("output", "completion_tokens"),
            ("total", "total_tokens"),
        ):
            value = getattr(usage, attr, None)
            if value is not None:
                detail[key] = value
        cached = getattr(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
        )
        if cached is not None:
            detail["cached"] = cached
        reasoning = getattr(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
        )
        if reasoning is not None:
            detail["reasoning"] = reasoning
        return detail

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            options=kwargs,
        )
        started = perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(*self._usage(response))
            text = response.choices[0].message.content or ""
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
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

        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            image_count=len(images),
            options=kwargs,
        )
        started = perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(*self._usage(response))
            text = response.choices[0].message.content or ""
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise


    def complete_json(
        self,
        prompt: str,
        schema: type[ModelT] | None = None,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """OpenAI 로 JSON 을 강제한다(형태 잠금).

        ``schema`` 가 오고 strict 로 표현 가능하면 ``response_format=json_schema(strict)``
        로 **JSON 형식·필수 필드·enum 을 provider 가 강제**한다(값 제약·교차 규칙은 이후
        우리 Pydantic 이 검증). 자유형 object 가 있어 strict 가 불가하거나 ``schema`` 가
        없으면 ``json_object`` 모드로 떨어져 **유효 JSON** 만 보장한다(형태는 프롬프트가
        지시). json_object 모드는 메시지에 "json" 이 있어야 하므로 한 줄 지시를 덧붙인다.
        """

        strict = to_strict_schema(schema) if schema is not None else None
        if strict is not None:
            return self.complete(
                prompt,
                system=system,
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "schema": strict,
                        "strict": True,
                    },
                },
                **kwargs,
            )

        json_prompt = f"{prompt}\n\n반드시 유효한 JSON 객체 하나만 출력하세요."
        return self.complete(
            json_prompt,
            system=system,
            temperature=temperature,
            response_format={"type": "json_object"},
            **kwargs,
        )


@register_provider
class GeminiProvider(LLMProvider):
    """Google Gemini provider (google-genai SDK)."""

    name = "gemini"
    supports_vision = True
    sdk_package = "google-genai"

    def _build_client(self):
        from google import genai

        return genai.Client(api_key=self.api_key)

    @staticmethod
    def _usage(response) -> tuple[int | None, int | None]:
        """Gemini 응답에서 (입력 토큰, 출력 토큰)을 뽑는다."""

        usage = getattr(response, "usage_metadata", None)
        return (
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
        )

    @staticmethod
    def _usage_detail(response) -> dict[str, int]:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return {}
        detail: dict[str, int] = {}
        for key, attr in (
            ("input", "prompt_token_count"),
            ("output", "candidates_token_count"),
            ("total", "total_token_count"),
            ("cached", "cached_content_token_count"),
            ("reasoning", "thoughts_token_count"),
        ):
            value = getattr(usage, attr, None)
            if value is not None:
                detail[key] = value
        return detail

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            **kwargs,
        )
        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            options=kwargs,
        )
        started = perf_counter()
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
            self._log_usage(*self._usage(response))
            text = response.text or ""
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
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
        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            image_count=len(images),
            options=kwargs,
        )
        started = perf_counter()
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=parts,
                config=config,
            )
            self._log_usage(*self._usage(response))
            text = response.text or ""
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise


    def complete_json(
        self,
        prompt: str,
        schema: type[ModelT] | None = None,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """Gemini 로 JSON 을 강제한다(형태 잠금).

        ``schema`` 가 오면 ``response_schema`` 로 넘겨 **JSON 형식·필수 필드·enum 을
        provider 가 강제**한다(google-genai 가 중첩 모델을 inline 처리한다. Gemini 는
        ``$ref`` 를 안 받으므로 dict 가 아니라 Pydantic 모델을 그대로 준다). 값 제약·교차
        규칙은 이후 우리 Pydantic 이 검증한다. ``schema`` 가 없으면 mime 만 지정해 **유효
        JSON** 만 보장한다.
        """

        if schema is not None:
            return self.complete(
                prompt,
                system=system,
                temperature=temperature,
                response_mime_type="application/json",
                response_schema=schema,
                **kwargs,
            )

        return self.complete(
            prompt,
            system=system,
            temperature=temperature,
            response_mime_type="application/json",
            **kwargs,
        )


@register_provider
class BedrockProvider(LLMProvider):
    """Amazon Bedrock provider (Converse API).

    OpenAI/Gemini 와 달리 provider 별 API key 가 아니라 AWS 자격증명 체인으로
    인증한다(`requires_api_key = False`). 자격증명은 boto3 기본 체인(~/.aws
    프로필·SSO, 환경변수, 배포 시 IAM 역할)에 맡기고, 만료·교체가 필요한 키를
    앱 설정/.env 에 두지 않는다. 모델은 `bedrock_model`(Nova 모델 id 또는
    크로스리전 추론 프로필 id)로 지정한다.

    Converse API 는 모델과 무관한 통일된 요청·응답 구조를 제공하므로 텍스트·이미지
    입력과 토큰 사용량(`usage`)을 일관되게 다룰 수 있다. Nova 모델은 멀티모달이라
    이미지(vision) 입력을 지원한다.
    """

    name = "bedrock"
    requires_api_key = False
    supports_vision = True
    sdk_package = "boto3"
    _INFERENCE_CONFIG_KEYS = frozenset({"maxTokens", "stopSequences", "topP"})

    def _build_client(self):
        import boto3

        # 로컬에서만 .env 의 프로필 이름으로 ~/.aws/credentials 를 읽는다. 배포
        # 환경에서는 값이 잘못 들어가도 무시하고 AWS 실행 역할의 임시 자격증명을 쓴다.
        profile = settings.bedrock_aws_profile.strip()
        if settings.app_env.lower() == "local" and profile:
            session = boto3.Session(profile_name=profile)
            return session.client(
                "bedrock-runtime", region_name=settings.bedrock_region
            )

        return boto3.client("bedrock-runtime", region_name=settings.bedrock_region)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            options=kwargs,
        )
        started = perf_counter()
        try:
            response = self._converse(
                [{"text": prompt}],
                system=system,
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(*self._usage(response))
            text = self._extract_text(response)
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise

    def complete_with_images(
        self,
        prompt: str,
        images: list[ImageInput],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        content: list[dict] = [{"text": prompt}]
        for image in images:
            content.append(
                {
                    "image": {
                        "format": self._image_format(image.mime_type),
                        "source": {"bytes": image.data},
                    }
                }
            )
        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            image_count=len(images),
            options=kwargs,
        )
        started = perf_counter()
        try:
            response = self._converse(
                content,
                system=system,
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(*self._usage(response))
            text = self._extract_text(response)
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise

    def complete_json(
        self,
        prompt: str,
        schema: type[ModelT] | None = None,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """Bedrock 에서 유효 JSON 을 받아온다.

        ``schema`` 가 오면 Converse **tool-use** 로 형태를 강제한다: 스키마를 도구의
        ``inputSchema`` 로 실은 도구 하나를 정의하고 ``toolChoice`` 로 그 도구 호출을
        강제한 뒤, ``toolUse.input`` 을 JSON 텍스트로 돌려준다. ``schema`` 가 없으면
        네이티브 JSON 모드가 없으므로 프롬프트 지시에 맡기는 passthrough 다(soft).

        주의: 강제 ``toolChoice`` 지원은 모델(Nova 등)·버전에 따라 다르므로 실제 모델로
        검증이 필요하다. 모델이 도구 대신 텍스트를 내면 텍스트를 그대로 돌려주고, 이후
        검증·재시도가 처리한다.
        """

        if schema is None:
            return self.complete(
                prompt, system=system, temperature=temperature, **kwargs
            )

        self._emit_llm_prompt(
            prompt,
            system=system,
            temperature=temperature,
            options={
                **kwargs,
                "structuredSchema": schema.model_json_schema(),
            },
        )
        started = perf_counter()
        try:
            response = self._converse_structured(
                prompt, schema, system=system, temperature=temperature, **kwargs
            )
            self._log_usage(*self._usage(response))
            text = self._extract_tool_use(response)
            self._emit_llm_response(started, response, text)
            return text
        except Exception as exc:
            self._emit_llm_failure(started, exc)
            raise

    def _converse_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        system: str | None,
        temperature: float,
        **kwargs,
    ) -> dict:
        """스키마를 도구로 실어 Converse tool-use 호출을 강제한다."""

        tool_name = schema.__name__
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool_name,
                        "description": f"{tool_name} 형식의 결과를 이 도구로 반환한다.",
                        "inputSchema": {"json": schema.model_json_schema(by_alias=True)},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": tool_name}},
        }
        return self._converse(
            [{"text": prompt}],
            system=system,
            temperature=temperature,
            toolConfig=tool_config,
            **kwargs,
        )

    def _converse(
        self,
        content: list[dict],
        *,
        system: str | None,
        temperature: float,
        **kwargs,
    ) -> dict:
        """Bedrock Converse 를 호출하고 provider 전용 추가 옵션을 전달한다.

        ``maxTokens``/``stopSequences``/``topP`` 는 편의를 위해 직접 받을 수 있고,
        boto3 형식의 ``inferenceConfig`` 로 전달해도 된다. 그 밖의 값은
        ``guardrailConfig``/``requestMetadata`` 같은 Converse 최상위 옵션으로 전달한다.
        system 은 있을 때만 넣는다(빈 리스트는 Bedrock 이 거부한다).
        """

        inference_config = {"temperature": temperature}
        inference_config.update(kwargs.pop("inferenceConfig", {}))
        for key in self._INFERENCE_CONFIG_KEYS:
            if key in kwargs:
                inference_config[key] = kwargs.pop(key)

        params: dict = dict(kwargs)
        params.update({
            "modelId": self.model,
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": inference_config,
        })
        if system:
            params["system"] = [{"text": system}]
        return self.client.converse(**params)

    @staticmethod
    def _extract_text(response: dict) -> str:
        """Converse 응답에서 assistant 텍스트 블록을 이어붙여 반환한다."""

        blocks = response["output"]["message"]["content"]
        return "".join(block["text"] for block in blocks if "text" in block)

    @staticmethod
    def _extract_tool_use(response: dict) -> str:
        """Converse 응답에서 ``toolUse.input`` 을 JSON 텍스트로 뽑는다.

        모델이 도구 대신 텍스트로 답했으면 텍스트를 그대로 돌려준다(검증·재시도가 처리).
        """

        blocks = response["output"]["message"]["content"]
        for block in blocks:
            if "toolUse" in block:
                return json.dumps(block["toolUse"]["input"], ensure_ascii=False)
        return "".join(block["text"] for block in blocks if "text" in block)

    @staticmethod
    def _usage(response: dict) -> tuple[int | None, int | None]:
        """Converse 응답의 usage 에서 (입력 토큰, 출력 토큰)을 뽑는다."""

        usage = response.get("usage") or {}
        return usage.get("inputTokens"), usage.get("outputTokens")

    @staticmethod
    def _usage_detail(response: dict) -> dict[str, int]:
        usage = response.get("usage") or {}
        detail: dict[str, int] = {}
        for key, attr in (
            ("input", "inputTokens"),
            ("output", "outputTokens"),
            ("total", "totalTokens"),
        ):
            value = usage.get(attr)
            if value is not None:
                detail[key] = value
        cache_read = usage.get("cacheReadInputTokens")
        cache_write = usage.get("cacheWriteInputTokens")
        if cache_read is not None or cache_write is not None:
            detail["cached"] = (cache_read or 0) + (cache_write or 0)
        return detail

    @staticmethod
    def _image_format(mime_type: str) -> str:
        """MIME 타입을 Converse 가 요구하는 포맷명(png/jpeg/gif/webp)으로 변환한다."""

        subtype = (mime_type or "image/jpeg").split("/")[-1].lower()
        return "jpeg" if subtype in ("jpg", "jpeg") else subtype


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
        return self.provider.complete(
            prompt,
            system=system,
            temperature=temperature,
            **kwargs,
        )

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
        return self.provider.complete_with_images(
            prompt,
            images,
            system=system,
            temperature=temperature,
            **kwargs,
        )

    def complete_json(
        self,
        prompt: str,
        schema: type[ModelT] | None = None,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """provider 가 유효 JSON 을 강제해 텍스트로 돌려준다(검증은 호출자 몫)."""

        return self.provider.complete_json(
            prompt,
            schema,
            system=system,
            temperature=temperature,
            **kwargs,
        )

    def complete_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_repairs: int = 1,
        **kwargs,
    ) -> ModelT:
        """응답을 ``schema`` 로 강제해 검증된 Pydantic 인스턴스를 반환한다."""

        logger.debug(
            "LLM structured 호출: provider=%s, model=%s, schema=%s",
            self.provider.name,
            self.provider.model,
            schema.__name__,
        )
        return self.provider.complete_structured(
            prompt,
            schema,
            system=system,
            temperature=temperature,
            max_repairs=max_repairs,
            **kwargs,
        )
