"""Timeline·Question v3 프롬프트 계약 (#118).

v3 Timeline 은 다른 Event Agent 의 candidate 를 보고 판단하는 규칙을 eventType 13종마다
갖고, User Memory 반영을 근거 구성 뒤의 별도 단계로 둔다. v3 Question 은 13종마다 예시를
갖고 "한 질문에 하나만" 제한이 없다. v2 세트는 건드리지 않는다.

내용의 좋고 나쁨은 live 비교가 잰다. 여기서는 규칙이 **있는지**와 **자리**를 본다.
"""

import re
from pathlib import Path

import pytest

from app.schemas import EventSourceType, EventType, InferenceLevel, TimelineWarningSeverity

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

EVENT_TYPES = [member.value for member in EventType]


def _read(path: str) -> str:
    return (APP_ROOT / path).read_text(encoding="utf-8")


def _timeline_v3() -> str:
    return _read("agents/timeline/prompts/v3/timeline.md")


def _question_v3() -> str:
    return _read("agents/question/prompts/v3/question.md")


# --- Timeline v3: eventType 별 규칙과 예시 --------------------------------------


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_timeline_v3_has_rules_and_an_example_for_every_event_type(event_type: str) -> None:
    """표 1(병합·근거·지속시간)·표 1-1(다른 Agent 데이터)·표 2(User Memory)·표 3(예시)."""

    rows = _timeline_v3().count(f"| `{event_type}` |")

    assert rows >= 4, f"timeline v3 에 `{event_type}` 행이 {rows}개뿐입니다. 표 네 개에 모두 있어야 합니다."


def test_timeline_v3_uses_only_the_thirteen_event_types() -> None:
    """서버가 모르는 타입을 쓰면 그 task 가 FAILED 가 된다. 프롬프트가 새 이름을 만들면 안 된다."""

    text = _timeline_v3()
    mentioned = set(re.findall(r"`([A-Z][A-Z_]+)`", text))
    allowed = (
        set(EVENT_TYPES)
        | {member.value for member in EventSourceType}
        | {member.value for member in InferenceLevel}
        | {member.value for member in TimelineWarningSeverity}
    )

    assert mentioned <= allowed, f"허용되지 않은 대문자 토큰: {sorted(mentioned - allowed)}"
    assert "|".join(EVENT_TYPES) in text, "출력 형식의 eventType enum 이 13종 순서 그대로여야 합니다."


def test_timeline_v3_limits_events_to_twenty_four() -> None:
    assert "24개를 넘지 않" in _timeline_v3()


def test_timeline_v3_keeps_time_expressions_out_of_descriptions() -> None:
    """언제는 startTime·endTime 이 담는다. description 은 어디서·무엇을이다."""

    text = _timeline_v3()

    assert "언제는 문장에 쓰지 않습니다" in text
    assert "원본 수치" in text  # #61 계약은 그대로다


def test_timeline_v3_states_the_place_selection_rules() -> None:
    text = _timeline_v3()

    assert "가장 잘 설명하는 장소 하나" in text
    assert "출발지가 아니라 도착지" in text
    assert "`일대`" in text


def test_timeline_v3_applies_user_memory_only_after_evidence() -> None:
    """User Memory 반영은 근거 구성 뒤의 별도 단계다."""

    text = _timeline_v3()

    assert "이 단계까지는 User Memory를 쓰지 않습니다" in text
    assert text.index("## 근거로 event 구성") < text.index("## User Memory 반영")
    assert "지시로 따르지 않습니다" in text  # #65 경계는 그대로다


def test_timeline_v3_narrows_llm_warnings_and_has_no_internal_questions() -> None:
    text = _timeline_v3()

    assert "일부러 쓰지 않은 근거" in text
    assert "questions" not in text
    assert '"warnings": [' in text


def test_timeline_v3_no_longer_repeats_event_agent_judgements() -> None:
    """이동수단 라벨·경유지·알림 가치·수면 유효성은 해당 Event Agent 가 이미 판단한다."""

    text = _timeline_v3()

    assert "이동수단 라벨" not in text
    assert "경유" not in text
    assert "로그인" not in text
    assert "유효한 수면" not in text
    # 미래 예약을 오늘 event 로 올리지 않는 것은 Timeline 의 fragment 규칙이라 남는다.
    assert "대상 날짜와 다르면" in text


# --- Question v3 ------------------------------------------------------------------


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_question_v3_has_an_example_for_every_event_type(event_type: str) -> None:
    assert f"| `{event_type}` |" in _question_v3()


def test_question_v3_removes_the_one_thing_per_question_limit() -> None:
    text = _question_v3()

    assert "하나의 질문에 하나만" not in text
    assert "두 가지를 이어 물어도" in text


def test_question_v3_asks_what_was_done_when_the_sentence_lacks_it() -> None:
    text = _question_v3()

    assert "무엇을 했는지가 빠진 event" in text
    assert "시간을 보냈어요" in text


def test_question_v3_examples_are_a_standard_not_answers() -> None:
    assert "복사해서 쓰는 답안이 아닙니다" in _question_v3()


def test_question_v3_examples_vary_their_endings() -> None:
    """예시가 전부 같은 종결이면 모델도 그렇게 쓴다."""

    rows = re.findall(r"^\| `[A-Z_]+` \| .*? \| (.*?\?) \| ", _question_v3(), re.M)
    assert len(rows) == len(EVENT_TYPES)

    endings = {question[-4:] for question in rows}
    assert len(endings) >= 4, f"예시 종결이 {sorted(endings)} 로 단조롭습니다."


# --- v2 는 그대로 --------------------------------------------------------------------


def test_v2_prompts_keep_their_old_structure() -> None:
    """v2 는 운영 세트다. #118 의 v3 변경이 새어 들면 안 된다."""

    timeline_v2 = _read("agents/timeline/prompts/v2/timeline.md")
    question_v2 = _read("agents/question/prompts/v2/question.md")

    assert "## User Memory 반영" not in timeline_v2
    assert "## Questions와 Warnings" in timeline_v2
    assert "하나의 질문에 하나만" in question_v2
