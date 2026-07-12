"""메인 에이전트 패키지.

입력 요청을 데이터별 Event Agent 로 분배해 event 후보/조각을 뽑고, 그 결과를
Timeline Agent 로 병합해 `TimelineDraft` 를 만드는 실행 흐름을 조율한다.
"""

from app.agents.main.main_agent import run_main_agent

__all__ = ["run_main_agent"]
