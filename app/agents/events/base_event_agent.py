"""데이터별 Event Agent 공통 인터페이스."""

from abc import abstractmethod

from app.agents.base import Agent
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import code_of_or, report_error
from app.core.execution_context import ExecutionStage
from app.core.logging import get_logger, log_fields, stage_span
from app.schemas import AgentEventResult, AgentWarning, TimelineDraftRequest
from app.services.fragment_guard import verify_agent_coverage
from app.services.source_integrity import filter_agent_result_sources
from app.services.source_lookup import raw_id_of
from app.services.validator import filter_result_to_window, resolve_window_bounds

logger = get_logger(__name__)


class EventAgent(Agent):
    """source type 별 입력을 해석해 event 후보와 불확실한 event 단서를 반환하는 Agent.

    ``source_attrs`` 는 그 Agent 가 읽는 요청 필드 이름이다. 보존 검사(#56 §8.5)가
    "이 Agent 에 전달된 raw item 이 후보나 단서로 남았는가" 를 판정하는 데 쓴다.
    비워 두면 그 Agent 는 검사에서 빠진다.
    """

    #: 이 Agent 가 소비하는 `TimelineDraftRequest` 필드 이름들.
    source_attrs: tuple[str, ...] = ()

    def generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        """요청에서 해당 source를 읽어 ``AgentEventResult``를 반환한다.

        LLM 호출, graph 실행, 응답 파싱 실패는 해당 Agent의 warning으로 흡수한다.
        개별 source Agent 실패가 전체 timeline 생성 흐름을 중단하지 않게 하기 위한 방어선이다.

        후보 단계에서 강제하는 것은 요청 window 하나뿐이다. 범위 밖 후보는 어차피
        timeline 에 올릴 수 없으므로 여기서 걷어 낸다. 그 밖의 정합성(근거 참조 등)은
        의미가 확정된 뒤 `draft_repair` 에서 다룬다.
        """

        with stage_span(
            logger,
            ExecutionStage.EVENT_AGENT,
            agent=self._agent_name(),
        ) as outcome:
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
                logger.debug(
                    "입력에 없는 rawId 참조 정리",
                    extra=log_fields(
                        **source_stats.violation_log_fields(
                            item_kind="CANDIDATE_OR_FRAGMENT"
                        )
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
            # window 밖 항목을 걷어 낸 **뒤에** 보존을 본다. 범위 밖 raw 가 빠진 것은
            # 유실이 아니라 정상이므로, 순서가 바뀌면 매번 거짓 경고가 붙는다.
            verify_agent_coverage(
                filtered,
                self._input_raw_ids(request),
                agent_name=self._agent_name(),
            )
            outcome["candidateCount"] = len(filtered.candidates)
            outcome["fragmentCount"] = len(filtered.fragments)
            outcome["warningCount"] = len(filtered.warnings)
            return filtered

    def _input_raw_ids(self, request: TimelineDraftRequest) -> set[str]:
        """이 Agent 가 실제로 받은 raw item 의 rawId 집합."""

        raw_ids: set[str] = set()
        for attr in self.source_attrs:
            for item in getattr(request, attr, None) or []:
                if (raw_id := raw_id_of(item)) is not None:
                    raw_ids.add(raw_id)
        return raw_ids

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
