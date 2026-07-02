"""Notification Event Agent 검증 (LLM 은 fake 주입)."""

from app.agents.events.notification import NotificationEventAgent
from app.schemas import EventSourceType
from tests.fixtures.fake_llm import FakeLLM, fragment, result_json
from tests.fixtures.requests import (
    DAY_START,
    HOUR,
    make_request,
    notification,
    notification_data,
)


def test_empty_notifications_skips_llm():
    fake = FakeLLM([result_json()])
    result = NotificationEventAgent(llm=fake).generate(make_request())
    assert result.fragments == []
    assert fake.calls == []


def test_missing_notifications_skips_llm():
    fake = FakeLLM([result_json()])
    req = make_request().model_copy(update={"notifications": None})
    result = NotificationEventAgent(llm=fake).generate(req)
    assert result.candidates == []
    assert result.fragments == []
    assert result.warnings == []
    assert fake.calls == []


def test_notification_inferred_as_fragment():
    final = result_json(fragments=[fragment("NOTIFICATION", "noti-1", "카카오톡 메시지")])
    fake = FakeLLM([final])
    req = make_request(
        notifications=notification_data(
            active=[notification("noti-1", "카카오톡", "새 메시지", DAY_START + HOUR)]
        )
    )

    result = NotificationEventAgent(llm=fake).generate(req)

    # 단일 호출 agent.
    assert len(fake.calls) == 1
    assert "라이프로그" in fake.calls[0].system
    assert "noti-1" in fake.calls[0].prompt
    assert result.candidates == []
    assert result.fragments[0].source_type is EventSourceType.NOTIFICATION
    assert result.fragments[0].source_id == "noti-1"
