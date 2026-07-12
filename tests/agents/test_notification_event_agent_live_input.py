"""Notification Event Agent 를 실제 notifications fixture 로 실행한다."""

import pytest

from app.agents.events.notification import NotificationEventAgent
from tests.agents.live_input_helpers import (
    assert_agent_result_shape,
    dump_agent_result,
    notification_request,
    prepare_live_llm_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


def test_notification_event_agent_with_real_input_fixture_writes_result() -> None:
    prepare_live_llm_env()

    result = NotificationEventAgent().generate(notification_request())
    actual = dump_agent_result("notification", result)

    assert_agent_result_shape(actual, result)
