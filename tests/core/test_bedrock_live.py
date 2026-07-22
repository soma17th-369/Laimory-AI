"""Bedrock 실제 호출 opt-in 테스트.

기본 실행에서는 skip 한다(네트워크/비용/AWS 자격증명 필요). 실행하려면:

- ``LAIMORY_LIVE_LLM=1``
- ``BEDROCK_MODEL=<Nova 모델 또는 크로스리전 추론 프로필 id>``, ``BEDROCK_REGION``
- 로컬 AWS 프로필(`BEDROCK_AWS_PROFILE`, 실제 키는 `~/.aws/credentials` 에 저장)

실제 호출이 텍스트를 돌려주고 토큰 사용량 로그가 남는지만 가볍게 확인한다.
Bedrock 호출 결과와 토큰 사용량 로그만 검증한다.
"""

import logging
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.live_llm
def test_bedrock_live_complete_returns_text_and_logs_tokens(caplog):
    load_dotenv(ROOT_DIR / ".env", override=False)

    if os.getenv("LAIMORY_LIVE_LLM") != "1":
        pytest.skip("실제 Bedrock 호출은 LAIMORY_LIVE_LLM=1 일 때만 실행합니다.")

    model = os.getenv("BEDROCK_MODEL")
    if not model:
        pytest.skip("BEDROCK_MODEL 이 설정되어야 실제 Bedrock 호출 테스트를 실행합니다.")

    from app.core.llm import BedrockProvider

    provider = BedrockProvider(model=model)

    with caplog.at_level(logging.INFO, logger="app.core.llm"):
        text = provider.complete(
            "한 문장으로 인사해줘.",
            system="너는 친절한 도우미다.",
            temperature=0.2,
        )

    assert isinstance(text, str) and text.strip(), "빈 응답"
    assert any(
        "토큰 사용량" in record.getMessage() and "provider=bedrock" in record.getMessage()
        for record in caplog.records
    ), "토큰 사용량 로그가 남지 않았습니다."
