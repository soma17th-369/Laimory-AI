"""app.core — 공통 인프라 패키지.

설정(`settings`), 로깅(`get_logger`), LLM 클라이언트(`LLMClient`)를
한곳에서 노출한다. 덕분에 다음처럼 짧게 가져다 쓸 수 있다.

    from app.core import LLMClient, get_logger, settings

사용 예시
--------

1) `.env` 의 `LLM_PROVIDER` 설정을 그대로 사용::

    from app.core import LLMClient

    client = LLMClient()
    answer = client.complete("한국어로 자기소개 해줘")
    print(answer)

2) provider / model 을 코드에서 직접 지정::

    gemini = LLMClient(provider="gemini")
    gpt = LLMClient(provider="openai", model="gpt-4o")

3) system 프롬프트와 파라미터 전달::

    summary = client.complete(
        "다음 문서를 3줄로 요약해줘: ...",
        system="너는 요약을 잘하는 도우미야.",
        temperature=0.2,
    )

4) 사용 가능한 provider 확인::

    from app.core import available_providers

    print(available_providers())  # ['gemini', 'openai']

5) 설정값과 로거 사용::

    from app.core import settings, get_logger

    logger = get_logger(__name__)
    logger.info("현재 provider=%s", settings.llm_provider)

FastAPI 라우터/서비스 안에서는 보통 다음과 같이 쓴다::

    from app.core import LLMClient

    def generate_draft(text: str) -> str:
        client = LLMClient()
        return client.complete(text, system="타임라인 초안을 작성해줘.")
"""

from app.core.config import settings
from app.core.llm import (
    LLMClient,
    LLMCompletion,
    TokenUsage,
    available_providers,
    get_provider,
)
from app.core.logging import get_logger

__all__ = [
    "settings",
    "get_logger",
    "LLMClient",
    "LLMCompletion",
    "TokenUsage",
    "get_provider",
    "available_providers",
]
