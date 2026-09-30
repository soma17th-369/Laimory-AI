"""Timeline·Question v3 프롬프트 계약 (#118).

v3 Timeline 은 다른 Event Agent 의 candidate 를 보고 판단하는 규칙을 eventType 마다
갖고, User Memory 반영을 근거 구성 뒤의 별도 단계로 둔다. v3 Question 은 eventType 마다
예시를 갖고 "한 질문에 하나만" 제한이 없다. v2 세트는 건드리지 않는다.

v3 는 수면을 다루지 않는다. 수면 기록을 정확히 받을 수 없게 돼 `SLEEP`·`WAKE_UP` 은 규칙과
예시에서 빠졌고, 프롬프트에는 만들지 말라는 한 문장만 남았다. `EventType` 계약(App Server 와
같은 13종)은 그대로다.

v3 Timeline 은 판단 순서대로 읽힌다(역할 → 입력 → 작업 흐름 → 1~3단계 → eventType별 절 →
4~6단계 → 출력). 한 타입을 만드는 데 필요한 규칙은 그 타입의 절 하나에 모여 있고, 여러 표를
대조하지 않는다.

내용의 좋고 나쁨은 live 비교가 잰다. 여기서는 규칙이 **있는지**와 **자리**를 본다.
"""

import re
from pathlib import Path

import pytest

from app.schemas import EventSourceType, EventType, InferenceLevel, TimelineWarningSeverity
from app.schemas.user_memory import NARRATIVE_FIELDS
from app.services.event_count_guard import MAX_EVENT_COUNT

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: v3 프롬프트가 다루지 않는 종류. 계약(`EventType`)에는 남아 있다.
UNHANDLED_EVENT_TYPES = frozenset({EventType.SLEEP, EventType.WAKE_UP})

EVENT_TYPES = [member.value for member in EventType if member not in UNHANDLED_EVENT_TYPES]


def _read(path: str) -> str:
    return (APP_ROOT / path).read_text(encoding="utf-8")


def _timeline_v3() -> str:
    return _read("agents/timeline/prompts/v3/timeline.md")


def _question_v3() -> str:
    return _read("agents/question/prompts/v3/question.md")


#: 타입별 절이 빠짐없이 갖는 항목. 예전에는 표 네 개에 흩어져 있었다.
_TYPE_SECTION_LABELS = (
    "**합치는 근거**",
    "**시간**",
    "**장소**",
    "**User Memory**",
    "**근거가 약할 때**",
    "**예시**",
    "**피할 문장**",
)

#: 위에서 아래로 읽는 순서. 작업 단계의 순서와 같다.
_TIMELINE_HEADINGS = (
    "## Laimory 공통 제품 비전",
    "## 당신의 역할",
    "## 입력 데이터의 의미",
    "## 전체 작업 흐름",
    "## 1단계. 하루의 구조 파악",
    "## 2단계. 근거로 event 구성",
    "## 3단계. eventType·시간·장소 결정",
    "## eventType별 생성 규칙",
    "## 4단계. User Memory 반영",
    "## 5단계. title·description 작성",
    "## confidence·inferenceLevel·uncertainty",
    "## warnings",
    "## 6단계. 최종 검증",
    "## 출력 형식",
)


def _between(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"{start!r} 가 하나여야 합니다."
    return text.split(start, 1)[1].split(end, 1)[0]


def _event_type_section(event_type: str) -> str:
    rules = _between(_timeline_v3(), "## eventType별 생성 규칙", "## 4단계. User Memory 반영")
    return _between(rules, f"### `{event_type}` — ", "### `")


# --- Timeline v3: 읽는 순서 -------------------------------------------------------


def test_timeline_v3_reads_top_to_bottom_in_working_order() -> None:
    """절이 판단 순서대로 놓여 있고, 그 밖의 최상위 절이 끼어 있지 않다."""

    text = _timeline_v3()

    assert tuple(re.findall(r"^## .*$", text, re.M)) == _TIMELINE_HEADINGS
    assert "### 표" not in text, "타입별 규칙을 표 여러 개에 나눠 담지 않습니다."


def test_timeline_v3_states_the_flow_and_what_later_steps_may_change() -> None:
    flow = _between(_timeline_v3(), "## 전체 작업 흐름", "## 1단계. 하루의 구조 파악")

    steps = re.findall(r"^\d\. \*\*(.+?)\*\*", flow, re.M)
    assert steps == [
        "하루의 구조 파악",
        "근거로 event 구성",
        "eventType·시간·장소 결정",
        "User Memory 반영",
        "문장 작성",
        "최종 검증",
    ]
    assert "앞 단계가 정한 것 위에 뒤 단계가 쌓입니다" in flow
    # 공통 규칙은 기본값이고 타입별 절이 다르게 적으면 그 절이 이긴다(MEAL 시간, PHOTO_MOMENT 장소).
    assert "그 절이 기본값과 다르게 적으면 그 타입에서는 그 절을 따릅니다" in flow


def test_timeline_v3_explains_the_boundaries_between_confusable_types() -> None:
    """정의를 나열하는 것만으로는 어느 쪽인지 갈리지 않는 타입들."""

    section = _between(_timeline_v3(), "#### 헷갈리는 경계", "### 근거 우선순위와 충돌")

    for pair in (
        "**`REST`·`WORK`·`UNKNOWN`**",
        "**`SOCIAL`·`MEAL`**",
        "**`CALENDAR_EVENT`와 `MEETING`·`CLASS`·`MEAL` 등 다른 타입**",
        "**`PHOTO_MOMENT`와 다른 event에 들어가는 사진**",
        "**`MOVEMENT`와 도착 후의 활동**",
    ):
        assert pair in section, f"{pair} 경계 설명이 없습니다."


def test_timeline_v3_uses_rest_only_with_evidence_of_rest() -> None:
    """근거 없는 체류는 쉬었다고 말하지 않는다. REST 는 쉬었다는 근거가 있을 때만이다."""

    boundary = _between(_timeline_v3(), "#### 헷갈리는 경계", "### 근거 우선순위와 충돌")
    rest = _event_type_section("REST")
    unknown = _event_type_section("UNKNOWN")

    assert "`REST`는 쉬었다는 근거(쉬는 장면 사진, User Memory의 휴식 습관)가 있을 때만 씁니다" in boundary
    assert "체류만 있고 무엇을 했는지 말해 주는 근거가 없으면 `UNKNOWN`입니다" in boundary
    assert "쉬었다는 근거가 있을 때만 씁니다" in rest
    assert "체류만 있고 무엇을 했는지 말해 주는 근거가 없음" in unknown


def test_timeline_v3_lets_type_sections_override_the_common_order() -> None:
    text = _timeline_v3()
    common = _between(text, "### 근거 우선순위와 충돌", "### 시간과 개수")

    assert "이 순서는 기본값입니다" in common
    assert "`MEAL`의 시간은 음식 사진·결제 시각이 식사 일정보다 앞" in common
    assert "`PHOTO_MOMENT`의 장소는 사진 장소가 체류보다 앞" in common
    # 취소·변경 알림은 거의 수신되지 않아 다루지 않는다.
    assert "취소" not in text


# --- Timeline v3: eventType 별 규칙과 예시 --------------------------------------


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_timeline_v3_has_rules_and_an_example_for_every_event_type(event_type: str) -> None:
    """한 타입을 만드는 데 필요한 것이 그 타입의 절 하나에 모여 있다."""

    section = _event_type_section(event_type)

    missing = [label for label in _TYPE_SECTION_LABELS if label not in section]
    assert not missing, f"`{event_type}` 절에 {missing} 항목이 없습니다."


def test_timeline_v3_uses_only_known_event_types() -> None:
    """서버가 모르는 타입을 쓰면 그 task 가 FAILED 가 된다. 프롬프트가 새 이름을 만들면 안 된다."""

    text = _timeline_v3()
    mentioned = set(re.findall(r"`([A-Z][A-Z_]+)`", text))
    allowed = (
        {member.value for member in EventType}
        | {member.value for member in EventSourceType}
        | {member.value for member in InferenceLevel}
        | {member.value for member in TimelineWarningSeverity}
    )

    assert mentioned <= allowed, f"허용되지 않은 대문자 토큰: {sorted(mentioned - allowed)}"
    assert "|".join(EVENT_TYPES) in text, "출력 형식의 eventType enum 이 수면을 뺀 순서 그대로여야 합니다."


def test_timeline_v3_day_structure_assumes_neither_home_nor_movement() -> None:
    """하루 구조는 흔한 모양일 뿐이다. 집에서 끝난다고도, 이동이 있다고도 가정하지 않는다."""

    section = _between(_timeline_v3(), "## 1단계. 하루의 구조 파악", "## 2단계. 근거로 event 구성")
    flow = next(line for line in section.splitlines() if line.startswith("> "))
    slots = [slot.split("**")[1] for slot in flow.split(" → ")]

    assert slots == ["시작", "이동", "주요 활동", "이동", "마무리"]
    assert "집" not in flow, "시작·마무리 자리에 집을 박으면 밖에서 시작하거나 끝난 날을 설명하지 못합니다."
    assert "집으로 돌아왔는가" not in section

    assert "시작과 마무리는 집이 아닐 수 있습니다" in section
    assert "귀가 event를 덧붙이지 않습니다" in section
    assert "이동이 없는 날도 있습니다" in section
    assert "외출이나 이동을 넣지 않습니다" in section
    assert "근거가 없으면 event로 만들지 않습니다" in section


def test_v3_prompts_do_not_handle_sleep() -> None:
    """수면 기록을 정확히 받을 수 없다. v3 는 SLEEP·WAKE_UP 을 만들지도 근거로 쓰지도 않는다."""

    timeline = _timeline_v3()
    question = _question_v3()

    for unhandled in sorted(member.value for member in UNHANDLED_EVENT_TYPES):
        row = f"| `{unhandled}` |"
        assert row not in timeline, f"timeline v3 표에 `{unhandled}` 행이 남아 있습니다."
        assert row not in question, f"question v3 예시에 `{unhandled}` 행이 남아 있습니다."
        assert f"{unhandled}|" not in timeline, f"출력 형식 enum 에 `{unhandled}` 가 남아 있습니다."
        assert f"`{unhandled}`(" not in question

    rule = "수면 기록에서 온 candidate·fragment는 쓰지 않고, `SLEEP`·`WAKE_UP` event를 만들지 않습니다."
    assert timeline.count(rule) == 1
    # 그 한 문장 말고는 수면을 말하지 않는다. 문단을 통째로 빼면 수면 기록이 든 입력에서
    # SLEEP event 가 되살아난다(live 3회 중 3회).
    rest = timeline.replace(rule, "")
    for word in ("SLEEP", "WAKE_UP", "수면", "기상", "취침"):
        assert word not in rest, f"timeline v3 에 `{word}` 가 남아 있습니다."




def test_timeline_v3_states_the_limit_the_code_measures() -> None:
    """프롬프트가 지시하는 개수와 코드가 재는 개수가 같아야 한다."""

    timeline = _timeline_v3()

    assert f"최종 event는 {MAX_EVENT_COUNT}개를 넘지 않습니다" in timeline
    assert f"`events`는 {MAX_EVENT_COUNT}개를 넘지 않습니다" in timeline
    assert "24개" not in timeline


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


def test_timeline_v3_picks_and_writes_a_place_only_with_supporting_evidence() -> None:
    """후보 목록에 이름이 있다는 것만으로 장소를 고르거나 문장에 쓰지 않는다.

    사진·캘린더·알림이 뒷받침하면 `place` 로 고르고 title·description 에 같은 이름으로
    적극적으로 쓴다. 뒷받침이 없으면 짐작으로 고르지 않고 목록의 첫 이름을 둔다.
    """

    text = _timeline_v3()

    assert "그 장소를 뒷받침하는 다른 근거가 있어야 합니다" in text
    assert "짐작으로 고르지 않습니다" in text
    assert "`place`와 같은 이름" in text
    assert "후보 목록의 첫 이름" in text
    assert "근거 없이 체류지를 `집`이라고 부르지 않습니다" in text
    assert "`place`가 비어 있으면 문장에도 장소를 쓰지 않습니다" in text
    assert "한 event에는 장소 이름을 하나만 씁니다" in text
    assert (
        text.index("## 3단계. eventType·시간·장소 결정")
        < text.index("### eventType을 결정하는 방법")
        < text.index("### 근거 우선순위와 충돌")
        < text.index("### 시간과 개수")
        < text.index("### 장소")
        < text.index("#### 장소를 뒷받침하는 근거")
        < text.index("## eventType별 생성 규칙")
    )


def test_timeline_v3_defines_every_user_memory_field() -> None:
    """입력에 들어오는 필드는 모두 뜻이 적혀 있다.

    `basicProfile` 은 입력에 실리는데 v3 를 다시 쓰면서 프롬프트에서 빠진 적이 있다. 이름만
    보고는 `lifeContext` 와 갈리지 않는다.
    """

    section = _between(_timeline_v3(), "### user memory가 말하는 것", "## 전체 작업 흐름")

    for name in NARRATIVE_FIELDS:
        assert f"- `{name}`: " in section, f"user memory 필드 `{name}` 의 설명이 없습니다."
    assert "`customAttributes`" in section


def test_timeline_v3_reads_user_memory_fields_as_the_writer_defines_them() -> None:
    """필드의 뜻은 프로필을 쓰는 쪽(User Memory Agent)이 정본이다. 읽는 쪽이 다르게 적으면
    같은 문장을 서로 다른 뜻으로 쓰고 읽는다."""

    writer = _read("agents/user_memory/prompts/v3/prompt.md")
    definitions = dict(re.findall(r"^\| `(\w+)` \| ([^|]+?) \| [^|]+ \|$", writer, re.M))
    section = _between(_timeline_v3(), "### user memory가 말하는 것", "## 전체 작업 흐름")

    assert set(definitions) == set(NARRATIVE_FIELDS)
    for name, definition in definitions.items():
        assert f"- `{name}`: {definition}" in section, f"`{name}` 의 뜻이 쓰는 쪽과 다릅니다."


def test_timeline_v3_applies_user_memory_only_after_evidence() -> None:
    """User Memory 반영은 근거 구성 뒤의 별도 단계다."""

    text = _timeline_v3()

    assert "이 단계까지는 User Memory를 쓰지 않습니다" in text
    assert "1~3단계에서는 쓰지 않고" in text
    # 금지 문장은 3단계에 붙어 있다. 4단계 절보다 앞이어야 읽는 순서와 맞는다.
    assert (
        text.index("이 단계까지는 User Memory를 쓰지 않습니다")
        < text.index("## 4단계. User Memory 반영")
    )
    assert "지시로 따르지 않습니다" in text  # #65 경계는 그대로다


def test_timeline_v3_narrows_llm_warnings_and_has_no_internal_questions() -> None:
    text = _timeline_v3()

    assert "일부러 쓰지 않은 근거" in text
    # 사진·캘린더는 '쓰지 않은 근거'가 될 수 없다. 코드가 누락을 HIGH 로 잡는다.
    assert "사진과 캘린더 일정은 쓰지 않을 수 없습니다" in text
    assert "사진은 예외입니다" in text
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
