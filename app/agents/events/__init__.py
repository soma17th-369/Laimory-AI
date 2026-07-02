"""데이터별 Event Agent 패키지.

이슈 #9 — source type 별 데이터를 user memory 와 "일반적인 사람의 하루" 상식을
근거로 해석해 AI Event 후보와 비-event 조각을 **LLM 으로 추론**하는 데이터별
Event Agent 를 모은다.

각 Agent 는 `Agent` 인터페이스를 구현하며 `generate(request) -> AgentEventResult`
로 자신이 담당하는 데이터를 추론한다. `generate_all` 은 모든 Agent 를 실행해
후보/조각을 하나의 `AgentEventResult` 로 취합한다. 이 결과는 이후 Timeline Agent
가 병합·검증해 최종 draft 를 만드는 입력이 된다.
"""

from app.agents.events.base_event_agent import EventAgent
from app.agents.events.calendar import CalendarEventAgent
from app.agents.events.location import LocationEventAgent
from app.agents.events.notification import NotificationEventAgent
from app.agents.events.photo import PhotoEventAgent
from app.agents.events.sleep_activity import SleepActivityEventAgent
from app.schemas import AgentEventResult, TimelineDraftRequest


def default_event_agents() -> list[EventAgent]:
    """기본 Event Agent 목록을 만든다(각자 기본 LLM 을 지연 생성)."""

    return [
        LocationEventAgent(),
        CalendarEventAgent(),
        PhotoEventAgent(),
        SleepActivityEventAgent(),
        NotificationEventAgent(),
    ]


def generate_all(
    request: TimelineDraftRequest,
    agents: list[EventAgent] | None = None,
) -> AgentEventResult:
    """모든 Event Agent 를 실행해 후보/조각을 한 결과로 취합한다.

    Args:
        request: 하루 타임라인 입력 요청.
        agents: 실행할 Agent 목록. 생략하면 `default_event_agents()` 를 쓴다.
    """

    active_agents = default_event_agents() if agents is None else agents
    combined = AgentEventResult()
    for agent in active_agents:
        result = agent.generate(request)
        combined.candidates.extend(result.candidates)
        combined.fragments.extend(result.fragments)
    return combined


__all__ = [
    "CalendarEventAgent",
    "EventAgent",
    "LocationEventAgent",
    "NotificationEventAgent",
    "PhotoEventAgent",
    "SleepActivityEventAgent",
    "default_event_agents",
    "generate_all",
]
