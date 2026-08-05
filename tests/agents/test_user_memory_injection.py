"""User Memory 가 모든 소비 Agent 에 같은 형태로 주입되는지 확인한다 (#65).

Agent 마다 User Memory 를 다르게 접거나 필드를 골라 쓰면, 같은 메모리를 보고도
서로 다른 문장을 읽는다. 그러면 어떤 판단이 무엇에 근거했는지 재현할 수 없고,
프롬프트를 고칠 때 어느 Agent 가 영향을 받는지도 알 수 없다.

여기서는 두 가지를 본다.

1. 6개 소비 Agent 가 모두 공용 projection(`user_memory_to_text`)을 쓴다.
2. 두 프롬프트 세트(v1/v2) 모두 User Memory 사용 원칙을 갖는다. 원칙이 한쪽에만
   있으면 `PROMPT_VERSION` 롤백이 경계를 지워 버린다.
"""

from pathlib import Path

import pytest

from app.agents.parsing import user_memory_to_text
from app.agents.timeline.timeline_agent import build_timeline_prompt
from app.schemas import UserMemory
from tests.fixtures.requests import make_request

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: User Memory 를 프롬프트에 싣는 Agent 모듈. 새 Agent 가 생기면 여기도 늘어난다.
_MEMORY_CONSUMERS = (
    "agents/events/calendar/agent.py",
    "agents/events/location/agent.py",
    "agents/events/notification/agent.py",
    "agents/events/photo/agent.py",
    "agents/events/sleep_activity/agent.py",
    "agents/timeline/timeline_agent.py",
)

#: 프롬프트 세트에 사용 원칙이 있어야 하는 Agent 디렉터리.
_MEMORY_PROMPTS = (
    ("agents/events/calendar", "prompt.md"),
    ("agents/events/location", "prompt.md"),
    ("agents/events/notification", "prompt.md"),
    ("agents/events/photo", "prompt.md"),
    ("agents/events/sleep_activity", "prompt.md"),
    ("agents/timeline", "timeline.md"),
)

_VERSIONS = ("v1", "v2")
_SECTION = "## User Memory 사용 원칙"


@pytest.mark.parametrize("module_path", _MEMORY_CONSUMERS)
def test_consumer_uses_the_shared_projection(module_path: str) -> None:
    source = (APP_ROOT / module_path).read_text(encoding="utf-8")

    assert "user_memory_to_text(" in source, (
        f"{module_path} 가 공용 projection 을 쓰지 않습니다. User Memory 를 직접 "
        "직렬화하면 Agent 마다 다른 문자열을 보게 됩니다."
    )


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("agent_dir,filename", _MEMORY_PROMPTS)
def test_prompt_set_states_the_usage_boundary(
    agent_dir: str, filename: str, version: str
) -> None:
    prompt = (APP_ROOT / agent_dir / "prompts" / version / filename).read_text(
        encoding="utf-8"
    )

    assert _SECTION in prompt, (
        f"{agent_dir}/{version}/{filename} 에 User Memory 사용 원칙이 없습니다. "
        f"PROMPT_VERSION={version} 으로 돌리면 경계가 사라집니다."
    )
    # 이 두 문장이 경계의 핵심이다. 없으면 절만 있고 규칙이 없는 것과 같다.
    assert "확정하지 않습니다" in prompt
    assert "원본 사실이 이깁니다" in prompt


def test_timeline_and_event_agents_receive_the_same_text() -> None:
    memory = UserMemory.model_validate(
        {"schemaVersion": "1.0", "basicProfile": "30대 개발자"}
    )
    request = make_request(user_memory=memory)
    projection = user_memory_to_text(request.user_memory)

    from app.agents.parsing import build_infer_prompt

    event_prompt = build_infer_prompt(projection, "없음", date=request.date)
    timeline_prompt = build_timeline_prompt(request, projection, "없음", "없음")

    assert projection in event_prompt
    assert projection in timeline_prompt
