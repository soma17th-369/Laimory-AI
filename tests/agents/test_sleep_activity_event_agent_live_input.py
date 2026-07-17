"""SleepActivity Event Agent 를 실제 health fixture 로 실행한다."""

import pytest

from app.agents.events.sleep_activity import SleepActivityEventAgent
from tests.agents.live_input_helpers import (
    assert_agent_result_shape,
    dump_agent_result,
    health_request,
    prepare_live_llm_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


def test_sleep_activity_event_agent_with_real_input_fixture_writes_result() -> None:
    prepare_live_llm_env()

    result = SleepActivityEventAgent().generate(health_request())
    actual = dump_agent_result("sleep_activity", result)

    assert_agent_result_shape(actual, result)
