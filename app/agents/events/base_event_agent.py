"""데이터별 Event Agent 공통 인터페이스."""

from abc import abstractmethod

from app.agents.base import Agent
from app.schemas import AgentEventResult, TimelineDraftRequest


class EventAgent(Agent):
    """source type 별 입력을 해석해 후보와 source fragment 를 반환하는 Agent."""

    @abstractmethod
    def generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        """요청에서 담당 source 를 읽어 `AgentEventResult` 를 반환한다."""
