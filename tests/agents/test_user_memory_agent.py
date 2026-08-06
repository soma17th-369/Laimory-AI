"""User Memory 갱신 Agent (#64).

프롬프트가 지켜야 하는 것 하나가 이 파일의 핵심이다. **AI 가 쓴 문장에서 사용자
성향을 뽑지 않는다.** 그 규칙이 프롬프트에서 사라지면 모델은 반드시 `title` 과
`subtitle` 에서 성격을 만들어 내고, 그 프로필이 다시 다음 타임라인 문장을 만드는 데
쓰여 스스로를 강화한다. 결과만 봐서는 알아채기 어려운 종류의 고장이다.
"""

from pathlib import Path

import pytest

from app.agents.user_memory import UserMemoryAgent, build_update_prompt
from app.schemas.user_memory import UserMemory
from app.services.user_memory_limits import build_daily_timeline_digest
from app.schemas.user_memory_update import DailyTimeline
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.user_memory import daily_timeline, daily_timeline_event, memory_json

_PROMPTS = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "agents"
    / "user_memory"
    / "prompts"
)


def _digest(events=None):
    payload = [daily_timeline(events=events if events is not None else [daily_timeline_event()])]
    return build_daily_timeline_digest([DailyTimeline.model_validate(item) for item in payload])


# --- 프롬프트 조립 -----------------------------------------------------


def test_prompt_carries_the_existing_profile_and_the_timelines():
    prompt = build_update_prompt(
        UserMemory(basic_profile="30대 개발자입니다."), _digest()
    )

    assert "[existing user memory]" in prompt
    assert "30대 개발자입니다." in prompt
    assert "[dailyTimelines]" in prompt


def test_missing_profile_reads_the_same_as_an_empty_one():
    """둘은 Agent 에게 구분할 이유가 없는 상태다."""

    none_prompt = build_update_prompt(None, _digest())
    empty_prompt = build_update_prompt(UserMemory(), _digest())

    assert none_prompt == empty_prompt
    assert "정보 없음" in none_prompt


def test_a_day_without_memo_is_told_so_explicitly():
    """빈 자리를 메우려는 것을 막는다. 알려 주지 않으면 AI 문장에서 성향을 만든다."""

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo=None)]))

    assert "[근거 없음]" in prompt
    assert "personality" in prompt
    assert "기존 값을 그대로" in prompt


def test_a_day_with_memo_gets_no_such_hint():
    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="오늘은 좋았어요.")]))

    assert "[근거 없음]" not in prompt
    assert "오늘은 좋았어요." in prompt


def test_violations_are_sent_back_without_quoting_values():
    prompt = build_update_prompt(
        None,
        _digest(),
        violations=["`personality` 에 PHONE 형태의 값이 그대로 남아 있습니다."],
    )

    assert "직전 출력이 규칙을 어겼습니다" in prompt
    assert "PHONE" in prompt


def test_prompt_never_contains_the_minute_of_an_event():
    prompt = build_update_prompt(
        None, _digest([daily_timeline_event(start_at="2026-08-04T12:43:00+09:00")])
    )

    assert "12:43" not in prompt


def test_prompt_never_contains_the_ai_written_question():
    """`question` 도 AI 가 쓴 문장이라 갱신 근거로 주지 않는다."""

    prompt = build_update_prompt(
        None, _digest([daily_timeline_event(question="어떤 이야기가 기억에 남았나요?")])
    )

    assert "기억에 남았나요" not in prompt


# --- 호출 -------------------------------------------------------------


def test_agent_returns_a_validated_memory():
    agent = UserMemoryAgent(llm=FakeLLM([memory_json(basicProfile="30대 개발자입니다.")]))

    memory = agent.generate(None, _digest())

    assert memory.basic_profile == "30대 개발자입니다."


def test_agent_sends_the_system_prompt():
    llm = FakeLLM([memory_json()])

    UserMemoryAgent(llm=llm).generate(None, _digest())

    assert "User Memory 갱신 시스템 프롬프트" in llm.calls[0].system


def test_over_length_field_is_repaired_by_the_structured_path():
    """필드 200자는 Pydantic 이 잡고, 교정 재시도가 한 번 더 묻는다."""

    llm = FakeLLM(
        [
            memory_json(basicProfile="가" * 201),
            memory_json(basicProfile="짧게 줄였습니다."),
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(None, _digest())

    assert memory.basic_profile == "짧게 줄였습니다."
    assert len(llm.calls) == 2


# --- 프롬프트 파일 계약 -------------------------------------------------


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("memo", "사용자의 실제 발화가 무엇인지 지목해야 합니다."),
        ("AI 가 센서 기록을 보고 대신 쓴 문장", "title/subtitle 의 출처를 밝혀야 합니다."),
        ("스스로를 강화", "왜 안 되는지를 설명해야 지시가 유지됩니다."),
        ("기존 값을 그대로 둡니다", "근거 없을 때의 동작이 명시돼야 합니다."),
        ("200자", "필드 길이 상한이 있어야 합니다."),
        ("customAttributes", "동적 속성 규칙이 있어야 합니다."),
        ("schemaVersion", "메타데이터를 출력하지 말라고 해야 합니다."),
    ],
)
def test_prompt_states_the_source_of_each_sentence(version: str, marker: str, why: str):
    text = (_PROMPTS / version / "prompt.md").read_text(encoding="utf-8")

    assert marker in text, f"user_memory {version} 프롬프트에 '{marker}' 가 없습니다. {why}"


def test_prompt_sets_stay_identical():
    """이 Agent 는 v1/v2 로 갈릴 이유가 없다. 갈리면 롤백이 다른 동작을 만든다."""

    v1 = (_PROMPTS / "v1" / "prompt.md").read_text(encoding="utf-8")
    v2 = (_PROMPTS / "v2" / "prompt.md").read_text(encoding="utf-8")

    assert v1 == v2
