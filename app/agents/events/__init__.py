"""데이터별 Event Agent 패키지.

이슈 #9 — source type 별 데이터를 user memory 와 "일반적인 사람의 하루" 상식을
근거로 해석해 AI Event 후보와 비-event 조각을 **LLM 으로 추론**하는 데이터별
Event Agent 를 모은다.

각 Agent 는 `Agent` 인터페이스를 구현하며 `generate(request) -> AgentEventResult`
로 자신이 담당하는 데이터를 추론한다. 실제 실행 흐름은 이후 pipeline/timeline
agent 에서 조율한다.
"""

from app.agents.events.base_event_agent import EventAgent
from app.agents.events.calendar import CalendarEventAgent
from app.agents.events.location import LocationEventAgent
from app.agents.events.notification import NotificationEventAgent
from app.agents.events.photo import PhotoEventAgent
from app.agents.events.sleep_activity import SleepActivityEventAgent


def default_event_agents() -> list[EventAgent]:
    """기본 Event Agent 목록을 만든다(각자 기본 LLM 을 지연 생성)."""

    return [
        LocationEventAgent(),
        CalendarEventAgent(),
        PhotoEventAgent(),
        SleepActivityEventAgent(),
        NotificationEventAgent(),
    ]


__all__ = [
    "CalendarEventAgent",
    "EventAgent",
    "LocationEventAgent",
    "NotificationEventAgent",
    "PhotoEventAgent",
    "SleepActivityEventAgent",
    "default_event_agents",
]
