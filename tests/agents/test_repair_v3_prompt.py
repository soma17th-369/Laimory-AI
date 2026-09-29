"""Repair v3 프롬프트 계약 (#119).

v3 Repair 는 코드가 확정한 뒤에 내용과 문장을 다듬는다. 코드가 이미 본 것을 다시 검증하지
않고, 코드가 찾은 것을 해소하고, 보정으로 어색해진 문장을 Timeline v3 기준으로 다시 쓴다.

Timeline 의 추론을 되돌리지 않게 하는 규칙이 함께 있다 — 구체적인 이름을 뭉개지 않고,
검토만 하는 warning 을 지우라는 신호로 읽지 않고, 재실행을 마지막 수단으로 둔다.

내용의 좋고 나쁨은 live 비교가 잰다. 여기서는 규칙이 **있는지**와 코드가 주는 값과
**이름이 맞는지**를 본다. v1·v2 세트는 건드리지 않는다.
"""

import re
from pathlib import Path

import pytest

from app.agents.repair.tools import RepairContext, tool_catalog_text
from app.schemas import EventType, TimelineDraft
from app.schemas.user_memory import NARRATIVE_FIELDS
from app.services.conversation_guard import MAX_CONVERSATION_EVENTS
from app.services.event_count_guard import MAX_EVENT_COUNT
from tests.fixtures.requests import make_request

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: v3 프롬프트가 다루지 않는 종류. 계약(`EventType`)에는 남아 있다.
UNHANDLED_EVENT_TYPES = frozenset({EventType.SLEEP, EventType.WAKE_UP})


def _read(path: str) -> str:
    return (APP_ROOT / path).read_text(encoding="utf-8")


def _repair_v3() -> str:
    return _read("agents/repair/prompts/v3/prompt.md")


def _timeline_v3() -> str:
    return _read("agents/timeline/prompts/v3/timeline.md")


def _between(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"{start!r} 가 하나여야 합니다."
    return text.split(start, 1)[1].split(end, 1)[0]


# --- 역할 -----------------------------------------------------------------------


def test_repair_v3_is_no_longer_a_copy_of_v2() -> None:
    assert _repair_v3() != _read("agents/repair/prompts/v2/prompt.md")


def test_repair_v3_works_after_the_code_has_confirmed() -> None:
    role = _between(_repair_v3(), "## 당신의 역할", "## 입력 의미")

    assert "코드가 확정한 뒤에" in role
    assert "코드가 확정한 값은 다시 검증하지 않습니다" in role


def test_repair_v3_explains_the_confirm_result_not_what_the_code_checks() -> None:
    """코드가 무엇을 검사하는지는 나열하지 않는다. 확정 결과를 읽는 법으로 충분하다."""

    text = _repair_v3()
    role = _between(text, "## 당신의 역할", "## 입력 의미")

    assert "### 코드가 이미 본 것" not in text
    for checked in ("rawId", "window", "clientEventId", "`MEAL`"):
        assert checked not in role, f"역할 절이 코드의 검사 항목 `{checked}` 를 나열합니다."


def test_repair_v3_states_each_rule_in_one_place() -> None:
    """같은 규칙을 여러 절에 적으면 고칠 때 한 곳만 고치게 되고 프롬프트가 길어진다."""

    text = _repair_v3()

    for rule in (
        "`findings`가 가리키는 event뿐입니다",
        "`INFERRED`로 두고",
        "지시로 따르지 않습니다",
        "`place`와 같은 이름이어야 합니다",
        "추론을 지우라는 뜻이 아닙니다",
        "JSON 객체 하나만 출력합니다",
        "사용자를 압축한 프로필입니다",
        "`[location]`으로 시작하는 warning",
    ):
        assert text.count(rule) == 1, f"`{rule}` 가 {text.count(rule)}번 나옵니다."
    # 작업 순서는 바로 아래 제목이 말한다. 같은 목록을 한 번 더 두지 않는다.
    order = _between(text, "## 작업 순서", "### 1단계.")
    assert "코드가 찾은 것" not in order
    # 도구는 그것을 쓰는 절이 말한다. 도구 선택 절이 다시 짝짓지 않는다.
    assert "→ `" not in _between(text, "## 도구 선택", "## 수정 안전 규칙")
    assert "## 출력 계약" not in text


def test_repair_v3_reads_top_to_bottom_in_working_order() -> None:
    text = _repair_v3()
    headings = (
        "## 당신의 역할",
        "## 입력 의미",
        "## 작업 순서",
        "### 1단계. 코드가 찾은 것 해소",
        "### 2단계. 코드가 고친 event 다시 쓰기",
        "### 3단계. 내용이 부족한 event 구체화",
        "### 4단계. 문장 다듬기",
        "## warning을 읽는 법",
        "## 도구 선택",
        "## 출력 형식",
    )

    positions = [text.index(heading) for heading in headings]

    assert positions == sorted(positions)


# --- 코드가 주는 값과 이름이 맞는가 ------------------------------------------------


@pytest.mark.parametrize(
    "section",
    ["자동 검사 결과", "event 근거", "user memory", "근거 원본", "사용 가능한 도구", "지금까지 실행한 도구"],
)
def test_repair_v3_explains_every_input_section(section: str) -> None:
    assert f"`[{section}]`" in _repair_v3()


@pytest.mark.parametrize(
    "key",
    [
        "corrected",
        "removed",
        "added",
        "findings",
        "confirm",
        "step",
        "changes",
        "candidates",
        "candidateIds",
        "unusedCandidates",
    ],
)
def test_repair_v3_names_every_key_the_code_sends(key: str) -> None:
    assert f"`{key}`" in _repair_v3(), f"코드가 싣는 키 `{key}` 의 설명이 없습니다."


@pytest.mark.parametrize(
    "kind",
    [
        "LONG_STAY_BETWEEN_MOVEMENTS",
        "DURATION_OVER_LIMIT",
        "CONVERSATION_EVENTS",
    ],
)
def test_repair_v3_says_what_to_do_for_every_finding_kind(kind: str) -> None:
    """코드가 내는 `kind` 는 프롬프트에 같은 이름으로 있어야 한다."""

    source = "\n".join(
        (APP_ROOT / path).read_text(encoding="utf-8")
        for path in ("services/draft_repair.py", "agents/repair/repair_agent.py")
    )

    assert f'"{kind}"' in source, f"코드가 `{kind}` 를 내지 않습니다."
    assert f"`{kind}`" in _repair_v3()


@pytest.mark.parametrize(
    "key",
    [
        "segments",
        "longStays",
        "shortStays",
        "outsideEventRawIds",
        "eventStartTime",
        "eventEndTime",
        "limitHours",
        "durationHours",
        "conversationEvents",
        "undeterminedEvents",
        "notificationCount",
    ],
)
def test_repair_v3_names_the_finding_fields_it_relies_on(key: str) -> None:
    assert f"`{key}`" in _repair_v3()


def test_repair_v3_names_only_tools_that_exist() -> None:
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul")
    catalog = tool_catalog_text(
        RepairContext(request=make_request(), draft=draft, extended=True)
    )
    tools = set(re.findall(r"^- `(\w+)\(", catalog, re.M))

    # 도구 이름은 소문자 snake_case 다. 입력 키는 camelCase, 검사 종류는 대문자다.
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", _repair_v3()))

    assert "split_event" in named
    assert named <= tools, f"없는 도구를 가리킵니다: {sorted(named - tools)}"


def test_repair_v3_uses_only_known_event_types() -> None:
    text = _repair_v3()
    handled = {member.value for member in EventType} - {
        member.value for member in UNHANDLED_EVENT_TYPES
    }
    listed = _between(text, "eventType은 11종 중 하나입니다: ", ".")

    assert set(re.findall(r"`(\w+)`", listed)) == handled


# --- 1단계. 코드가 찾은 것 --------------------------------------------------------


def test_repair_v3_splits_a_long_stay_without_exception() -> None:
    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "20분을 넘으면 예외가 없습니다" in section
    assert "역·터미널·공항" in section
    assert "`split_event`" in section
    assert "앞 이동, 체류, 뒤 이동" in section


def test_repair_v3_splits_only_what_the_findings_point_at() -> None:
    """실제 LLM 이 20분 이하 체류를 낀 이동까지 나눠 6분짜리 체류 카드를 만들었다.

    검사 종류마다 되풀이하지 않고 1단계 머리에 한 번 적는다.
    """

    text = _repair_v3()
    head = _between(text, "### 1단계.", "#### `LONG_STAY_BETWEEN_MOVEMENTS`")
    section = _between(
        text, "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )
    warnings = _between(text, "## warning을 읽는 법", "## 문제 분류")

    assert "나누거나 지우는 event는 `findings`가 가리키는 event뿐입니다" in head
    assert "20분 이하 체류를 묶은 하나의 이동" in section
    # 후보에 대한 warning 은 나눌 event 를 가리키지 않는다.
    assert "`[location]`으로 시작하는 warning" in warnings
    assert "나눌 event를 가리키지 않습니다" in warnings


def test_repair_v3_takes_the_split_times_from_the_segments() -> None:
    """실제 LLM 이 근거 원본의 시간으로 조각을 만들어 event 밖으로 나갔다."""

    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "`segments`의 `startAt`·`endAt`을 그대로 씁니다" in section
    assert "`segments`마다 조각 하나를 만듭니다" in section
    assert "조각의 시간으로 쓰지 않습니다" in section
    assert "그 근거로는 조각을 만들지 않습니다" in section


def test_repair_v3_makes_no_piece_for_a_short_stay_away_from_movements() -> None:
    """실제 LLM 이 하루를 뭉갠 event 를 나누며 8분·6분짜리 체류 카드를 만들었다."""

    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "`shortStays`는 이동과 이어지지 않은 20분 이하 체류입니다" in section
    assert "조각을 따로 만들지 않습니다" in section


def test_repair_v3_divides_the_evidence_not_only_the_time() -> None:
    """시간만 줄이면 이동 근거가 남아 코드가 시간을 다시 늘린다."""

    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "시간만 줄이지 않습니다" in section
    assert "일정 시간을 따릅니다" in section


def test_repair_v3_keeps_every_piece_within_the_limit() -> None:
    """실제 LLM 이 9시간짜리 근무를 3시간과 6시간으로 나눠 상한 초과가 그대로 남았다."""

    section = _between(_repair_v3(), "#### `DURATION_OVER_LIMIT`", "#### `CONVERSATION_EVENTS`")

    assert "나눈 조각도 각각 상한 안에 들어야 합니다" in section
    assert "제목으로 구분합니다" in section


def test_repair_v3_does_not_mistake_its_own_tool_log_for_the_code() -> None:
    """실제 LLM 이 자기가 부른 도구를 코드가 한 일로 읽고, 남은 위반을 두고 끝냈다."""

    text = _repair_v3()

    assert "앞 차례에 **당신이** 부른 도구" in text
    assert "`findings`에 남아 있으면 아직 해소되지 않은 것" in text


def test_repair_v3_does_not_cut_at_a_fragment_of_a_stay() -> None:
    """실제 LLM 이 15시간짜리 근무를 8분·10분짜리 체류 기록의 경계에서 나눴다."""

    section = _between(_repair_v3(), "#### `DURATION_OVER_LIMIT`", "#### `CONVERSATION_EVENTS`")

    assert "몇 분짜리 체류 기록의 시작과 끝은 경계가 아닙니다" in section
    assert "고르게 나눕니다" in section


def test_repair_v3_splits_an_over_limit_stay_in_the_same_call() -> None:
    """다음 차례로 미루면 반복 횟수를 쓴다. 반복은 세 번뿐이다."""

    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "같은 호출에서" in section


def test_repair_v3_picks_conversations_by_importance_not_by_count() -> None:
    section = _between(_repair_v3(), "#### `CONVERSATION_EVENTS`", "#### 검사끼리 부딪힐 때")

    assert f"하루 최대 {MAX_CONVERSATION_EVENTS}개" in section
    assert "알림이 많다고 중요한 대화는 아닙니다" in section
    # 코드가 대화인지 모르는 event 는 내용을 읽고 정한다.
    assert "대화인지 먼저 정합니다" in section
    # 지우기만 하면 누구와 연락했는지가 하루에서 사라진다.
    assert "그 event의 문장에 담습니다" in section


def test_repair_v3_orders_the_checks_that_pull_in_opposite_directions() -> None:
    section = _between(_repair_v3(), "#### 검사끼리 부딪힐 때", "### 2단계.")

    assert "보다 먼저입니다" in section
    # 프롬프트가 말하는 개수와 코드가 재는 개수가 같아야 한다.
    assert f"event 개수 {MAX_EVENT_COUNT}개 초과" in section
    assert f"event가 {MAX_EVENT_COUNT}개를 넘으면" in section
    assert "24개" not in section
    # 기존 장거리 여정 검사는 사이의 체류 길이를 보지 않는다. 합치라는 쪽으로 센다.
    assert "장거리 이동을 하나로 묶지 않음" in section
    assert "방금 나눈 조각을 다시 합치지 않습니다" in section


# --- 2단계. 코드가 고친 것 --------------------------------------------------------


def test_repair_v3_recovers_content_that_a_merge_dropped() -> None:
    section = _between(_repair_v3(), "### 2단계.", "### 3단계.")

    assert "합쳐지기 전 event의 내용" in section
    assert "남은 event의 문장에 담습니다" in section
    assert "되살리지 않습니다" in section


def test_repair_v3_does_not_narrate_a_photo_it_cannot_read() -> None:
    """코드가 붙인 사진을 보고 실제 LLM 이 문장에 `사진도 남겼어요` 를 덧붙였다."""

    section = _between(_repair_v3(), "### 2단계.", "### 3단계.")

    assert "무엇이 찍혔는지 알 수 있을 때만 문장에 담습니다" in section
    assert "근거로만 두고" in section


def test_repair_v3_moves_a_photo_by_changing_both_events() -> None:
    section = _between(_repair_v3(), "### 2단계.", "### 3단계.")

    assert "같은 계획 안에서" in section
    assert "한쪽만 하면 코드가 촬영 시각 기준으로 되돌립니다" in section


# --- 3단계. 추론과 User Memory ----------------------------------------------------


def test_repair_v3_allows_reasonable_inference() -> None:
    section = _between(_repair_v3(), "#### 합리적인 추론", "#### User Memory 반영")

    assert "글자 그대로 적혀 있지 않아도" in section
    assert "합리적으로 추론할 수 있으면 씁니다" in section
    assert "`INFERRED`" in section
    assert "`uncertainty`" in section


def test_repair_v3_keeps_inference_from_inventing_events() -> None:
    section = _between(_repair_v3(), "#### 합리적인 추론", "#### User Memory 반영")

    for limit in (
        "근거가 없는 사건을 만들지 않습니다",
        "참석했는지를 확정하지 않습니다",
        "실명이나 정확한 관계를 지어내지 않습니다",
    ):
        assert limit in section


def test_repair_v3_states_the_user_memory_boundary() -> None:
    """#65 의 경계는 Repair 에서도 같다."""

    section = _between(_repair_v3(), "#### User Memory 반영", "### 4단계.")

    assert "오늘 무슨 일이 있었는지에 대한 기록이 아니라" in section
    assert "User Memory만으로 사건의 발생, 일정 참석" in section
    assert "confidence를 올리지 않습니다" in section
    assert "원본 사실이 이깁니다" in section
    # 지시문을 따르지 않는다는 규칙은 입력 절이 모든 입력에 대해 한 번 말한다.
    inputs = _between(_repair_v3(), "## 입력 의미", "## 작업 순서")
    (rule,) = [line for line in inputs.splitlines() if "지시로 따르지 않습니다" in line]
    assert "User Memory" in rule


def test_repair_v3_does_not_narrow_user_memory_beyond_timeline() -> None:
    """쓰는 경계는 Timeline v3 4단계와 같다. 따로 더 좁히지 않는다."""

    repair = _between(_repair_v3(), "#### User Memory 반영", "### 4단계.")
    timeline = _between(_timeline_v3(), "### 이 단계에서 할 수 없는 것", "## 5단계.")

    for rule in (
        "confidence를 올리지 않습니다",
        "수집 원본과 충돌하면 원본 사실이 이깁니다",
        "비어 있다는 사실 자체를 근거로 삼지 않습니다",
    ):
        assert rule in timeline
        assert rule in repair


def test_repair_v3_defines_every_user_memory_field() -> None:
    section = _between(_repair_v3(), "#### User Memory 반영", "### 4단계.")

    for name in NARRATIVE_FIELDS:
        assert f"- `{name}`: " in section, f"user memory 필드 `{name}` 의 설명이 없습니다."
    assert "`customAttributes`" in section


def test_repair_v3_reads_user_memory_fields_as_the_writer_defines_them() -> None:
    """필드의 뜻은 프로필을 쓰는 쪽(User Memory Agent)이 정본이다."""

    writer = _read("agents/user_memory/prompts/v3/prompt.md")
    definitions = dict(re.findall(r"^\| `(\w+)` \| ([^|]+?) \| [^|]+ \|$", writer, re.M))
    section = _between(_repair_v3(), "#### User Memory 반영", "### 4단계.")

    assert set(definitions) == set(NARRATIVE_FIELDS)
    for name, definition in definitions.items():
        assert f"- `{name}`: {definition}" in section, f"`{name}` 의 뜻이 쓰는 쪽과 다릅니다."


# --- 4단계. 문장 ----------------------------------------------------------------


def test_repair_v3_does_not_blur_the_names_timeline_wrote() -> None:
    """#118 live 에서 Repair 가 일정 제목으로 쓴 활동 이름을 `업무` 로 바꿨다."""

    section = _between(
        _repair_v3(), "#### Timeline이 쓴 구체적인 이름은 뭉개지 않습니다", "#### 말투와 길이"
    )

    assert "더 넓은 말로 바꾸지 않습니다" in section
    assert "`업무`" in section
    assert "틀려졌을 때만 고칩니다" in section


def test_repair_v3_keeps_time_expressions_out_of_descriptions_only() -> None:
    """Timeline v3 는 이 규칙을 `description` 에만 적용하고 제목 예시는 `오전 근무` 다."""

    section = _between(_repair_v3(), "#### 말투와 길이", "#### 장소")

    assert "`description`에 시간 표현을 쓰지 않습니다" in section
    assert "이 규칙은 `description`에 한합니다" in section
    assert "`회사에서 오전 근무`" in section
    assert "`회사에서 오전 근무`" in _timeline_v3()


def test_repair_v3_examples_do_not_teach_time_expressions() -> None:
    """예전 예시의 고친 쪽이 `점심 무렵…`·`저녁 무렵…` 이었다. 그것이 되살아났다."""

    section = _between(_repair_v3(), "#### 예시", "## warning을 읽는 법")

    for line in section.splitlines():
        if "→" not in line:
            continue
        fixed = line.split("→", 1)[1]
        for expression in ("무렵", "아침에", "저녁에", "오전부터", "밤늦게"):
            assert expression not in fixed, f"고친 예시에 시간 표현이 있습니다: {line}"


def test_repair_v3_keeps_the_place_name_the_same_as_place() -> None:
    section = _between(_repair_v3(), "#### 장소", "#### 문장에 쓰지 않는 것")

    assert "`place`와 같은 이름" in section
    assert "`place`가 비어 있으면" in section


# --- warning 과 도구 ------------------------------------------------------------


def test_repair_v3_reads_warnings_in_three_groups() -> None:
    """나누지 않으면 추론을 허용한다는 지시와 warning 이 서로 반대로 말한다."""

    section = _between(_repair_v3(), "## warning을 읽는 법", "## 문제 분류")
    must, rest = section.split("검토만 하는 것:", 1)
    review, ignore = rest.split("고칠 것이 아닌 것:", 1)

    # 검사 결과와 같은 warning 은 1단계가 다룬다. 종류를 여기서 다시 나열하지 않는다.
    for item in ("`findings`와 같은 내용", "민감정보", "120자"):
        assert item in must
    for item in ("장시간 체류", "대화로 만든 event"):
        assert item not in must
    for item in ("단서만을 근거로", "어디에도 없는 장소명", "관계 호칭"):
        assert item in review
    assert "추론을 지우라는 뜻이 아닙니다" in review
    assert "코드가 고쳤다고 알리는 warning" in ignore


def test_repair_v3_does_not_call_a_tool_that_changes_nothing() -> None:
    """실제 LLM 이 event 아홉 개를 같은 문장으로 다시 썼다."""

    text = _repair_v3()

    assert "값이 바뀌지 않는 호출은 하지 않습니다" in text
    assert "이번에 도구로 고칠 것만" in text


def test_repair_v3_treats_rerun_as_the_last_resort() -> None:
    section = _between(_repair_v3(), "## 도구 선택", "## 수정 안전 규칙")

    assert "재실행은 마지막 수단입니다" in section
    assert "받지 못합니다" in section
    assert "모두 사라집니다" in section
    assert "제한 시간" in section


def test_repair_v3_does_not_handle_sleep() -> None:
    text = _repair_v3()

    assert "`SLEEP`·`WAKE_UP` event를 만들지 않습니다" in text
    assert "직접 기록된 수면" not in text
    assert "기상" not in text


def test_repair_v3_does_not_handle_cancel_notifications() -> None:
    """취소·변경 알림은 거의 수신되지 않아 Timeline v3 도 다루지 않는다."""

    text = _repair_v3()

    assert "취소" not in text
    assert "스킵" not in text


# --- Timeline v3 의 두 곳 -------------------------------------------------------


def test_timeline_v3_no_longer_says_repair_cannot_see_candidates() -> None:
    text = _timeline_v3()

    assert "candidate는 보지 못합니다" not in text
    assert "event가 참조한 candidate를 보지만" in text
    # 그래도 Timeline 만 아는 판단은 남겨야 한다.
    assert "일부러 쓰지 않은 근거" in text


@pytest.mark.parametrize("event_type", ["WORK", "SOCIAL", "MEETING", "CLASS"])
def test_timeline_v3_follows_the_calendar_before_the_limit(event_type: str) -> None:
    """코드는 일정대로인 event 를 상한에서 뺀다. 프롬프트가 다르게 말하면 Timeline 이
    일정대로인 event 를 쪼개고 코드는 그것을 문제 삼지 않는다."""

    rules = _between(_timeline_v3(), "## eventType별 생성 규칙", "## 4단계. User Memory 반영")
    section = _between(rules, f"### `{event_type}` — ", "### `")
    (time_line,) = [line for line in section.splitlines() if line.startswith("- **시간**")]

    assert "일정 시간을 따르고, 일정이 없으면 최대" in time_line


# --- v2 는 그대로 ----------------------------------------------------------------


def test_v2_repair_keeps_its_old_structure() -> None:
    """v2 는 운영 세트다. #119 의 v3 변경이 새어 들면 안 된다."""

    repair_v2 = _read("agents/repair/prompts/v2/prompt.md")
    timeline_v2 = _read("agents/timeline/prompts/v2/timeline.md")

    assert "## 검증 우선순위" in repair_v2
    assert "### 5. Photo 귀속" in repair_v2
    assert "자동 검사 결과" not in repair_v2
    assert "split_event" not in repair_v2
    assert "event가 참조한 candidate를 보지만" not in timeline_v2
