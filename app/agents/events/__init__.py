"""데이터별 Event Agent 패키지.

이슈 #9 — source type 별 데이터를 user memory 와 "일반적인 사람의 하루" 상식을
근거로 해석해 AI Event 후보와 비-event 조각을 **LLM 으로 추론**하는 데이터별
Event Agent 를 모은다.

각 Agent 는 `Agent` 인터페이스를 구현하며 `generate(request) -> AgentEventResult`
로 자신이 담당하는 데이터를 추론한다. 실제 실행 흐름은 이후 메인 에이전트에서
조율한다.
"""

from app.agents.events.base_event_agent import EventAgent
from app.agents.events.calendar import CalendarEventAgent
from app.agents.events.location import LocationEventAgent
from app.agents.events.notification import NotificationEventAgent
from app.agents.events.photo import PhotoEventAgent
from app.agents.events.sleep_activity import SleepActivityEventAgent
from app.schemas import AgentEventResult, TimelineDraftRequest
from app.services.place_resolver import resolve_candidate_places


def default_event_agents() -> list[EventAgent]:
    """기본 Event Agent 목록을 만든다(각자 기본 LLM 을 지연 생성)."""

    return [
        LocationEventAgent(),
        CalendarEventAgent(),
        PhotoEventAgent(),
        SleepActivityEventAgent(),
        NotificationEventAgent(),
    ]


def merge_event_results(
    results: list[AgentEventResult], request: TimelineDraftRequest
) -> AgentEventResult:
    """Event Agent 결과들을 하나의 `AgentEventResult` 로 취합한다.

    main agent 의 취합 node 와, Event Agent 를 다시 돌린 뒤 Timeline Agent 를 재실행
    하는 Repair Agent 도구가 함께 쓴다. 두 곳이 서로 다르게 취합하면 재실행 결과가
    처음 결과와 다른 규칙으로 병합된다.

    취합 직후 candidate 의 장소 정보를 입력에서 복사한다(이슈 #72). **이 자리인 이유**가
    바로 위 문단이다 — Timeline Agent 로 들어가는 fan-in 이 여기 하나뿐이라, 여기서 채워야
    최초 실행과 Repair 재실행이 같은 입력을 본다. main graph 의 node 에만 두면
    `rerun_timeline_agent` 경로에서 장소가 빠진다.
    """

    merged = AgentEventResult()
    for result in results:
        merged.candidates.extend(result.candidates)
        merged.fragments.extend(result.fragments)
        merged.warnings.extend(result.warnings)
    resolve_candidate_places(merged, request)
    return merged


__all__ = [
    "CalendarEventAgent",
    "EventAgent",
    "LocationEventAgent",
    "NotificationEventAgent",
    "PhotoEventAgent",
    "SleepActivityEventAgent",
    "default_event_agents",
    "merge_event_results",
]
