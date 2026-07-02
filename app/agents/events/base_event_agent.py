"""데이터별 Event Agent 공통 인터페이스."""

from abc import abstractmethod

from app.agents.base import Agent
from app.core.logging import get_logger
from app.schemas import AgentEventResult, TimelineDraftRequest

logger = get_logger(__name__)


class EventAgent(Agent):
    """source type 별 입력을 해석해 후보와 source fragment 를 반환하는 Agent."""

    def generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        """요청에서 담당 source 를 읽어 `AgentEventResult` 를 반환한다.

        LLM 호출, graph 실행, 응답 파싱 실패는 해당 Agent 의 경고로 흡수한다.
        한 source Agent 실패가 전체 이벤트 후보 생성 흐름을 중단하지 않게 하기
        위한 방어선이다.
        """

        try:
            return self._generate(request)
        except Exception as exc:
            logger.warning(
                "Event Agent 실행 실패: agent=%s, error=%s",
                self.name,
                exc,
                exc_info=True,
            )
            return AgentEventResult(
                warnings=[
                    {
                        "agentName": self.name or self.__class__.__name__,
                        "message": f"{self.name or self.__class__.__name__} agent 실행 실패: {exc}",
                    }
                ]
            )

    @abstractmethod
    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        """실제 Agent 구현. 실패 처리는 `generate()` 가 담당한다."""
