"""Calendar Event Agent 를 실제 calendar fixture 로 실행한다."""

import pytest

from app.agents.events.calendar import CalendarEventAgent
from tests.agents.live_input_helpers import (
    assert_agent_result_shape,
    calendar_request,
    dump_agent_result,
    prepare_live_llm_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


def test_calendar_event_agent_with_real_input_fixture_writes_result() -> None:
    prepare_live_llm_env()

    result = CalendarEventAgent().generate(calendar_request())
    actual = dump_agent_result("calendar", result)

    assert_agent_result_shape(actual, result)
