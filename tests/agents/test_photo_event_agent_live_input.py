"""Photo Event Agent 를 실제 photos fixture(+실제 이미지 바이너리)로 실행한다."""

import pytest

from tests.agents.live_input_helpers import (
    assert_agent_result_shape,
    dump_agent_result,
    photo_request,
    prepare_live_llm_env,
    vision_photo_event_agent,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


def test_photo_event_agent_with_real_input_fixture_writes_result() -> None:
    prepare_live_llm_env()

    # data/input 의 실제 JPEG 를 vision 으로 보며 describe → infer 를 수행한다.
    result = vision_photo_event_agent().generate(photo_request())
    actual = dump_agent_result("photo", result)

    assert_agent_result_shape(actual, result)
