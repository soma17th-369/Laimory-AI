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
import uuid
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


def _location_request():
    """하루치 위치 입력. 구조화 출력이 실제로 깨지던 크기다(#98)."""

    from app.schemas import TimelineDraftRequest

    places = ["집", "회사", "카페", "식당", "헬스장", "지하철역", "마트", "공원"]
    stays, movements = [], []
    minute = 6 * 60
    for i in range(10):
        s_start, s_end = minute, minute + 20
        m_start, m_end = s_end, s_end + 10
        minute = m_end
        stays.append({
            "rawId": str(uuid.UUID(int=i * 2 + 1)),
            "startAt": f"2026-06-20T{s_start // 60:02d}:{s_start % 60:02d}:00+09:00",
            "endAt": f"2026-06-20T{s_end // 60:02d}:{s_end % 60:02d}:00+09:00",
            "latitude": 37.4 + i * 0.001,
            "longitude": 127.0 + i * 0.001,
            "place": places[i % len(places)],
            "address": f"서울특별시 강남구 테헤란로 {i + 1}",
            "places": [places[i % len(places)]],
            "durationText": "20분",
        })
        movements.append({
            "rawId": str(uuid.UUID(int=i * 2 + 2)),
            "startAt": f"2026-06-20T{m_start // 60:02d}:{m_start % 60:02d}:00+09:00",
            "endAt": f"2026-06-20T{m_end // 60:02d}:{m_end % 60:02d}:00+09:00",
            "start": {"latitude": 37.4 + i * 0.001, "longitude": 127.0 + i * 0.001},
            "end": {"latitude": 37.4 + (i + 1) * 0.001, "longitude": 127.0 + (i + 1) * 0.001},
            "durationText": "10분",
            "distanceMeters": 900 + i * 10,
            "transports": ["WALK" if i % 2 else "SUBWAY"],
        })
    return TimelineDraftRequest.model_validate({
        "taskId": "live-test-98",
        "date": "2026-06-20",
        "timezone": "Asia/Seoul",
        "window": {"start": "2026-06-20T06:00:00+09:00",
                   "end": "2026-06-20T23:59:59+09:00"},
        "stays": stays,
        "movements": movements,
    })


@pytest.mark.integration
@pytest.mark.live_llm
def test_bedrock_live_location_agent_produces_candidates():
    """실제 `AgentEventResult` 스키마로 구조화 출력이 성립하는지 확인한다 (#98).

    `maxTokens` 를 싣지 않던 시절 같은 입력이 `stopReason=malformed_tool_use` 로
    빈 응답을 돌려주며 후보 0건이 됐다. 이 테스트는 그 회귀를 잡는다.
    """

    load_dotenv(ROOT_DIR / ".env", override=False)

    if os.getenv("LAIMORY_LIVE_LLM") != "1":
        pytest.skip("실제 Bedrock 호출은 LAIMORY_LIVE_LLM=1 일 때만 실행합니다.")
    if not os.getenv("BEDROCK_MODEL"):
        pytest.skip("BEDROCK_MODEL 이 설정되어야 실제 Bedrock 호출 테스트를 실행합니다.")

    from app.agents.events.location.agent import LocationEventAgent
    from app.core.execution_context import execution_context, structured_failures

    with execution_context("live-test-98"):
        result = LocationEventAgent().generate(_location_request())
        failures = structured_failures()

    assert not failures, f"구조화 출력이 실패했습니다: {failures}"
    assert result.candidates, (
        "후보가 0건입니다. 구조화 출력이 빈 응답을 돌려줬을 수 있습니다: "
        f"warnings={[w.message for w in result.warnings]}"
    )
