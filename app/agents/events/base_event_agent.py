"""데이터별 Event Agent 공통 인터페이스."""

from abc import abstractmethod

from app.agents.base import Agent
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import code_of_or, report_error
from app.core.logging import get_logger
from app.core.observability import (
    ObservationEventType,
    ObservationStage,
    emit_observation,
    observation_scope,
)
from app.schemas import AgentEventResult, AgentWarning, TimelineDraftRequest
from app.services.source_integrity import filter_agent_result_sources
from app.services.validator import filter_result_to_window, resolve_window_bounds

logger = get_logger(__name__)


class EventAgent(Agent):
    """source type 별 입력을 해석해 event 후보와 불확실한 event 단서를 반환하는 Agent."""

    def generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        """요청에서 해당 source를 읽어 ``AgentEventResult``를 반환한다.

        LLM 호출, graph 실행, 응답 파싱 실패는 해당 Agent의 warning으로 흡수한다.
        개별 source Agent 실패가 전체 timeline 생성 흐름을 중단하지 않게 하기 위한 방어선이다.

        후보 단계에서 강제하는 것은 요청 window 하나뿐이다. 범위 밖 후보는 어차피
        timeline 에 올릴 수 없으므로 여기서 걷어 낸다. 그 밖의 정합성(근거 참조 등)은
        의미가 확정된 뒤 `draft_repair` 에서 다룬다.
        """

        with observation_scope(
            ObservationStage.EVENT_AGENT, agent=self._agent_name()
        ):
            emit_observation(
                ObservationEventType.STARTED,
                payload={
                    "request": request.model_dump(by_alias=True, mode="json"),
                },
            )
            try:
                result = self._generate(request)
            except Exception as exc:
                failure_code = code_of_or(exc, ErrorCode.EVENT_AGENT_FAILED)
                report_error(
                    logger,
                    failure_code,
                    "Event Agent 실행 실패",
                    exc=exc,
                    context={"agent": self.name},
                    exc_info=True,
                )
                agent_name = self.name or self.__class__.__name__
                return AgentEventResult(
                    warnings=[
                        {
                            "agentName": agent_name,
                            "message": (
                                f"{agent_name} agent 실행 실패: "
                                f"{message_for(failure_code)}"
                            ),
                        }
                    ]
                )

            filtered, source_stats = filter_agent_result_sources(result, request)
            if source_stats.changed:
                emit_observation(
                    ObservationEventType.VALIDATION_REPAIRED,
                    payload=source_stats.observation_payload(
                        item_kind="CANDIDATE_OR_FRAGMENT"
                    ),
                )
                filtered.warnings.append(
                    AgentWarning(
                        agent_name=self._agent_name(),
                        message=(
                            "입력에 없는 rawId 참조 "
                            f"{source_stats.removed_refs}건을 제거하고, 유효한 근거가 "
                            f"남지 않은 후보/단서 {source_stats.dropped_items}건을 "
                            "제외했습니다."
                        ),
                    )
                )
            filtered = self._enforce_window(request, filtered)
            emit_observation(
                ObservationEventType.COMPLETED,
                payload={
                    "candidateCount": len(filtered.candidates),
                    "fragmentCount": len(filtered.fragments),
                    "warningCount": len(filtered.warnings),
                    "result": filtered.model_dump(by_alias=True, mode="json"),
                },
            )
            return filtered

    def _agent_name(self) -> str:
        return self.name or self.__class__.__name__

    def _enforce_window(
        self, request: TimelineDraftRequest, result: AgentEventResult
    ) -> AgentEventResult:
        """요청 window 밖 후보/단서를 제거하고, 제거가 있으면 warning 을 남긴다."""

        bounds = resolve_window_bounds(request)
        if bounds is None:
            return result

        filtered, dropped = filter_result_to_window(result, bounds)
        if dropped:
            filtered.warnings.append(
                AgentWarning(
                    agent_name=self._agent_name(),
                    message=f"요청 시간 범위(window) 밖 후보/단서 {dropped}건을 제외했습니다.",
                )
            )
        return filtered

    @abstractmethod
    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        """실제 Agent 구현. 실패 처리는 ``generate()``가 담당한다."""
