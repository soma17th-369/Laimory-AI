"""공통 에이전트 인터페이스.

모든 에이전트(데이터별 Event Agent, Timeline Agent 등)가 공유하는 최소 계약을
둔다. 세부 산출물 타입은 하위 인터페이스에서 좁힌다.
"""

from abc import ABC, abstractmethod

from app.schemas import TimelineDraftRequest


class Agent(ABC):
    """모든 에이전트가 구현하는 공통 인터페이스."""

    #: Agent 식별 이름(로깅·디버깅용).
    name: str = ""

    @abstractmethod
    def generate(self, request: TimelineDraftRequest):
        """요청을 받아 에이전트 산출물을 반환한다."""
