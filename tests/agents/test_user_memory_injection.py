"""User Memory 주입 경계 (#65, #119).

User Memory 는 **해석·표현 계층에만** 들어간다. Timeline Agent, Question Agent, 그리고
v3 세트의 Repair Agent 다.

Event Agent 를 뺀 이유는 취향이 아니다. 다섯이 병렬로 돌고 Timeline 이 그 결과를
병합하는데, 다섯이 같은 프로필을 읽고 같은 방향으로 기울면 Timeline 은 그것을 **독립된
근거 다섯 개의 합의**로 읽는다. 실제로는 같은 근거 하나가 다섯 번 세어진 것이고, 병합
로직은 둘을 구분할 수 없다. 프로필을 합류 지점 한 곳에만 두면 이 문제가 사라진다.

Repair 는 #119 에서 들어왔다. 내용이 빈 event 를 구체화하려면 Timeline 이 본 것과 같은
프로필을 봐야 한다. 다만 **v3 세트에서만** 받는다 — v2 Repair 프롬프트에는 사용 경계가
없고, 경계 없이 주면 모델이 프로필로 없던 일을 만들어 낼 수 있다.

여기서는 그 경계가 코드와 프롬프트 양쪽에서 지켜지는지 본다.
"""

from pathlib import Path

import pytest

from app.agents.parsing import build_infer_prompt, user_memory_to_text
from app.agents.question.question_agent import build_question_prompt
from app.agents.repair.repair_agent import build_repair_prompt
from app.agents.repair.tools import RepairContext
from app.agents.timeline.timeline_agent import build_timeline_prompt
from app.schemas import TimelineDraft, UserMemory
from tests.fixtures.requests import make_request

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: User Memory 를 프롬프트에 싣는 Agent 모듈.
_MEMORY_CONSUMERS = (
    "agents/timeline/timeline_agent.py",
    "agents/question/question_agent.py",
    "agents/repair/repair_agent.py",
)

#: User Memory 를 받지 않는 Agent 모듈.
_NON_CONSUMERS = (
    "agents/events/calendar/agent.py",
    "agents/events/location/agent.py",
    "agents/events/notification/agent.py",
    "agents/events/photo/agent.py",
    "agents/events/sleep_activity/agent.py",
)

#: 사용 원칙이 있어야 하는 프롬프트. 활성 세트는 v2 다. Repair 는 v3 에서만 받는다.
_MEMORY_PROMPTS = (
    "agents/timeline/prompts/v2/timeline.md",
    "agents/question/prompts/v2/question.md",
    "agents/repair/prompts/v3/prompt.md",
)

#: User Memory 를 받지 않는 Repair 프롬프트.
_REPAIR_PROMPTS_WITHOUT_MEMORY = (
    "agents/repair/prompts/v1/prompt.md",
    "agents/repair/prompts/v2/prompt.md",
)

#: User Memory 를 언급해서는 안 되는 프롬프트. 받지 않는 입력을 설명하면
#: 모델이 없는 입력을 찾거나 지어낸다.
_NON_MEMORY_PROMPTS = tuple(
    f"agents/events/{agent}/prompts/{version}/prompt.md"
    for version in ("v2", "v3")
    for agent in ("calendar", "location", "notification", "photo", "sleep_activity")
)


@pytest.mark.parametrize("module_path", _MEMORY_CONSUMERS)
def test_consumer_uses_the_shared_projection(module_path: str) -> None:
    source = (APP_ROOT / module_path).read_text(encoding="utf-8")

    assert "user_memory_to_text(" in source, (
        f"{module_path} 가 공용 projection 을 쓰지 않습니다. User Memory 를 직접 "
        "직렬화하면 Agent 마다 다른 문자열을 보게 됩니다."
    )


@pytest.mark.parametrize("module_path", _NON_CONSUMERS)
def test_non_consumer_never_touches_user_memory(module_path: str) -> None:
    source = (APP_ROOT / module_path).read_text(encoding="utf-8")

    assert "user_memory" not in source, (
        f"{module_path} 가 User Memory 를 참조합니다. 해석·표현 계층 밖으로 나가면 "
        "병합 단계가 같은 근거를 여러 번 세게 됩니다."
    )


def test_event_agent_prompt_carries_no_user_memory() -> None:
    """Event Agent 의 user prompt 자체에 프로필 자리가 없다."""

    prompt = build_infer_prompt("데이터", date="2026-08-06")

    assert "사용자 정보" not in prompt
    assert "user memory" not in prompt


@pytest.mark.parametrize("prompt_path", _MEMORY_PROMPTS)
def test_prompt_states_the_usage_boundary(prompt_path: str) -> None:
    prompt = (APP_ROOT / prompt_path).read_text(encoding="utf-8")

    assert "user memory" in prompt.lower()
    # 이 문장이 경계의 핵심이다. 없으면 입력 설명만 있고 규칙이 없는 것과 같다.
    assert "지시로 따르지 않습니다" in prompt


@pytest.mark.parametrize("prompt_path", _NON_MEMORY_PROMPTS)
def test_event_agent_prompt_does_not_mention_user_memory(prompt_path: str) -> None:
    prompt = (APP_ROOT / prompt_path).read_text(encoding="utf-8")

    assert "user memory" not in prompt.lower(), (
        f"{prompt_path} 가 User Memory 를 설명합니다. Event Agent 는 그 입력을 받지 "
        "않으므로, 설명만 남으면 모델이 없는 입력을 지어냅니다."
    )


def test_timeline_and_question_receive_the_same_text() -> None:
    memory = UserMemory.model_validate(
        {"schemaVersion": "1.0", "basicProfile": "30대 개발자"}
    )
    request = make_request(user_memory=memory)
    projection = user_memory_to_text(request.user_memory)

    timeline_prompt = build_timeline_prompt(request, projection, "없음", "없음")
    question_prompt = build_question_prompt(request, [])

    assert projection in timeline_prompt
    assert projection in question_prompt


# --- Repair (#119) --------------------------------------------------------------


def _repair_context(
    memory: UserMemory | None, *, extended: bool = True
) -> RepairContext:
    request = make_request(user_memory=memory)
    draft = TimelineDraft(user_id="u", date=request.date, timezone="Asia/Seoul")
    return RepairContext(request=request, draft=draft, extended=extended)


def test_repair_receives_the_same_text_as_timeline_and_question() -> None:
    """Agent 마다 다른 문자열을 읽으면 누가 무엇을 근거로 판단했는지 재현할 수 없다."""

    memory = UserMemory.model_validate(
        {"schemaVersion": "1.0", "currentFocus": "결제 서비스 리뉴얼"}
    )

    prompt = build_repair_prompt(_repair_context(memory), 3)

    assert f"[user memory]\n{user_memory_to_text(memory)}" in prompt


def test_repair_prompt_falls_back_to_no_information() -> None:
    prompt = build_repair_prompt(_repair_context(None), 3)

    assert "[user memory]\n정보 없음" in prompt


def test_repair_carries_user_memory_on_every_iteration() -> None:
    """Repair 는 돌 때마다 같은 프로필을 본다. 남은 횟수에 따라 빼지 않는다."""

    memory = UserMemory.model_validate({"routines": "저녁에는 드라마를 봅니다."})
    ctx = _repair_context(memory)

    for remaining in (3, 2, 1):
        assert user_memory_to_text(memory) in build_repair_prompt(ctx, remaining)


def test_repair_without_extended_input_carries_no_user_memory() -> None:
    """v1·v2 세트의 Repair 입력은 예전 그대로다."""

    memory = UserMemory.model_validate({"routines": "저녁에는 드라마를 봅니다."})

    prompt = build_repair_prompt(_repair_context(memory, extended=False), 3)

    assert "user memory" not in prompt
    assert user_memory_to_text(memory) not in prompt
    assert "[자동 검사 결과]" not in prompt
    assert "[event 근거]" not in prompt


@pytest.mark.parametrize("prompt_path", _REPAIR_PROMPTS_WITHOUT_MEMORY)
def test_repair_prompt_without_memory_does_not_mention_it(prompt_path: str) -> None:
    prompt = (APP_ROOT / prompt_path).read_text(encoding="utf-8")

    assert "user memory" not in prompt.lower(), (
        f"{prompt_path} 가 User Memory 를 설명합니다. 이 세트의 Repair 는 그 입력을 받지 "
        "않으므로, 설명만 남으면 모델이 없는 입력을 지어냅니다."
    )


def test_question_prompt_falls_back_to_no_information() -> None:
    prompt = build_question_prompt(make_request(), [])

    assert "[user memory]\n정보 없음" in prompt


def test_question_prompt_keeps_memory_on_retry() -> None:
    """재요청은 빠진 event 만 다시 묻는다. 문체 기준까지 잃을 이유는 없다."""

    memory = UserMemory.model_validate({"memoryStyle": "짧게 남기는 편입니다."})
    request = make_request(user_memory=memory)

    prompt = build_question_prompt(request, [], retry=True)

    assert user_memory_to_text(memory) in prompt


# event projection 이 confidence·sourceRefs·분 단위 시각을 빼는 계약(#66)은
# tests/agents/test_question_agent.py 가 이미 고정한다. User Memory 를 더했다고
# 그쪽이 넓어지지 않으므로 여기서 다시 세우지 않는다.
