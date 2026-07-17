"""Location Event Agent 를 실제 STAY/MOVEMENT 입력으로 실행한다."""

import pytest

from app.agents.events.location import LocationEventAgent
from tests.agents.live_input_helpers import (
    assert_agent_result_shape,
    dump_agent_result,
    prepare_live_llm_env,
    stay_request,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


def test_location_event_agent_with_real_input_fixture_writes_result() -> None:
    prepare_live_llm_env()

    result = LocationEventAgent().generate(stay_request())
    actual = dump_agent_result("location", result)

    assert_agent_result_shape(actual, result)
