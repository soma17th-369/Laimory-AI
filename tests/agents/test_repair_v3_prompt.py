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
from app.services.event_count_guard import MAX_EVENT_COUNT
from tests.fixtures.requests import make_request

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

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
        "나누는 이유는 길이가 아니라 근거입니다",
        "근거가 아닌 것:",
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
        "LOCATION_ONLY_OVERLAP",
        "STAY_WORD_IN_TITLE",
        "TIME_IN_TITLE",
        "ADDRESS_IN_NARRATION",
    ],
)
def test_repair_v3_says_what_to_do_for_every_finding_kind(kind: str) -> None:
    """코드가 내는 `kind` 는 프롬프트에 같은 이름으로 있어야 한다."""

    source = "\n".join(
        (APP_ROOT / path).read_text(encoding="utf-8")
        for path in ("services/draft_repair.py", "services/narrative_place_guard.py", "agents/repair/repair_agent.py")
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
        "overlappingEvents",
        "coverRatio",
        "found",
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
    """수면도 다룬다(#134). 목록은 계약의 13종 그대로다."""

    listed = _between(_repair_v3(), "eventType은 13종 중 하나입니다: ", ".")

    assert set(re.findall(r"`(\w+)`", listed)) == {member.value for member in EventType}


# --- 1단계. 코드가 찾은 것 --------------------------------------------------------


def test_repair_v3_splits_a_long_stay_without_exception() -> None:
    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "20분을 넘으면 예외가 없습니다" in section
    assert "역·터미널·공항" in section
    # 연속 체류를 하나로 두는 원칙(#134)이 이동 분할을 깨지 않는다. 사용자가 정한 것이다.
    assert "같은 캠퍼스 안에서 옮겨 다닌 것이어도 나눕니다" in section
    assert "`split_event`" in section
    assert "앞 이동, 체류, 뒤 이동" in section


def test_repair_v3_splits_by_evidence_not_by_length_or_findings() -> None:
    """나누는 조건은 길이도 `findings` 도 아니고 흡수된 사건의 근거다(#134).

    `findings` 로만 묶으면 검토 기준(12시간) 아래의 5시간 `WORK` 에 흡수된 회의를 Repair 가
    보고도 나누지 못한다. 반대로 #119 live 에서 20분 이하 체류를 낀 이동까지 나눠 6분짜리
    체류 카드를 만든 적이 있어, 근거가 아닌 것을 같은 자리에 적는다.
    """

    text = _repair_v3()
    head = _between(text, "### 1단계.", "#### `LONG_STAY_BETWEEN_MOVEMENTS`")
    section = _between(
        text, "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )
    warnings = _between(text, "## warning을 읽는 법", "## 문제 분류")

    assert "`findings`가 가리키는 event뿐" not in text
    assert "`findings`가 가리키지 않은 event라도" in head
    assert "길이만으로는 어떤 event도 나누지 않습니다" in head
    assert "자기 시간을 가진 별도 사건" in head
    (not_evidence,) = [line for line in head.splitlines() if "근거가 아닌 것:" in line]
    for item in ("몇 분짜리 체류 기록", "20분 이하 체류", "지점 이름 차이", "오전·오후"):
        assert item in not_evidence
    assert "이동 없이 이어진 체류는 길이·장소명과 무관하게 하나입니다" in head
    assert "`LONG_STAY_BETWEEN_MOVEMENTS`가 먼저입니다" in head
    assert "20분 이하 체류를 묶은 하나의 이동" in section
    # live 에서 본 잘못된 분할(#134): event 자신의 근거 알림을 0분짜리 조각으로 떼어 냈고,
    # 이미 있는 식사·회의를 한 벌 더 만들었고, 이동 분할 뒤 캠퍼스 체류를 지점마다 나눴다.
    assert "그 event 자신을 설명하는 알림" in not_evidence
    assert "이미 있는지 봅니다" in head
    assert "문장에만 담고 끝내지 않고 나눕니다" in head
    assert "`STAY` segment들은 하나의 체류라 한 조각으로 묶습니다" in section
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


def test_repair_v3_reads_duration_as_a_review_signal() -> None:
    """#119 는 넘으면 나누라고 했고 실제 LLM 이 경계 없는 9.5시간 체류를 다섯 조각으로
    고르게 나눴다(#134). 이제 검토 신호이고, 묻힌 사건이 없으면 그대로 둔다."""

    section = _between(_repair_v3(), "#### `DURATION_OVER_LIMIT`", "#### 검사끼리 부딪힐 때")

    assert "나누라는 뜻이 아니라" in section
    assert "길이와 무관하게 그대로 둡니다" in section
    assert "「근거인 것」" in section
    assert "남아 있어도 됩니다" in section
    for removed in ("고르게 나눕니다", "상한 안에 들어야 합니다", "오후 근무"):
        assert removed not in section, f"길이로 나누라는 지시 `{removed}` 가 남아 있습니다."


def test_repair_v3_does_not_mistake_its_own_tool_log_for_the_code() -> None:
    """실제 LLM 이 자기가 부른 도구를 코드가 한 일로 읽고, 남은 위반을 두고 끝냈다."""

    text = _repair_v3()

    assert "앞 차례에 **당신이** 부른 도구" in text
    assert "`findings`에 남아 있으면 아직 해소되지 않은 것" in text


def test_repair_v3_does_not_split_a_stay_piece_for_its_length() -> None:
    """이동으로 나눈 체류 조각을 상한에 맞춰 또 나누라는 지시가 있었다(#134 에서 뺐다)."""

    section = _between(
        _repair_v3(), "#### `LONG_STAY_BETWEEN_MOVEMENTS`", "#### `DURATION_OVER_LIMIT`"
    )

    assert "상한" not in section
    assert "같은 호출에서" not in section


def test_repair_v3_leaves_the_conversation_limit_to_the_code() -> None:
    """대화 event 하루 3개는 코드가 알림 수로 맞춘다. Repair 가 고를 것이 없다.

    Repair 에 맡겼을 때 실제 LLM 은 알림 수 순서로 지우라는 지시를 4번 중 3번 따르지 않았다.
    """

    text = _repair_v3()

    assert "CONVERSATION_EVENTS" not in text
    assert "CONVERSATION_OVER_LIMIT" not in text
    assert "notificationCount" not in text
    # 코드가 지운 대화를 Repair 가 되살리면 다음 확정에서 다시 지워진다.
    section = _between(text, "### 2단계.", "### 3단계.")
    assert "대화로 만든 event는 코드가 하루 3개로 맞춥니다" in section
    assert "되살리지 않습니다" in section


def test_repair_v3_orders_the_checks_that_pull_in_opposite_directions() -> None:
    section = _between(_repair_v3(), "#### 검사끼리 부딪힐 때", "### 2단계.")

    assert "보다 먼저입니다" in section
    # 프롬프트가 말하는 개수와 코드가 재는 개수가 같아야 한다.
    assert f"event 개수 {MAX_EVENT_COUNT}개 초과" in section
    assert f"event가 {MAX_EVENT_COUNT}개를 넘으면" in section
    assert "24개" not in section
    # 기존 장거리 여정 검사는 사이의 체류 길이를 보지 않는다. 합치라는 쪽으로 센다.
    assert "장거리 이동을 하나로 묶지 않음" in section
    # 연속 체류와 10개 제한이 길이 분할보다 먼저다. 근거 없이 나뉜 조각은 되합친다(#134).
    # 예전에는 재병합을 막아 dev trace 의 마지막 반복에서도 12개가 남았다.
    assert "길이를 맞추려는 분할보다 먼저입니다" in section
    assert "다시 합치지 않습니다" not in section
    assert "근거 없이 나뉜 체류 조각" in section
    assert "문장만 고치고 끝내지 않고" in section
    # 병합 도구가 없다. 있는 도구로 합치는 방법을 적는다.
    assert "`update_event`로 넓히고" in section
    assert "`delete_event`로 지웁니다" in section


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
    # 쓰는 쪽은 항목마다 절 하나를 갖고 그 첫 줄이 정의다(`- **담는 것**: …`).
    # `customAttributes` 는 고정 필드가 아니라 읽는 쪽이 따로 설명한다.
    definitions = {
        name: definition
        for name, definition in re.findall(
            r"^### `(\w+)` — .+\n\n- \*\*담는 것\*\*: (.+)$", writer, re.M
        )
        if name != "customAttributes"
    }
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
    section = _between(_repair_v3(), "#### 장소\n", "#### 문장에 쓰지 않는 것")

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


def test_repair_v3_turns_a_restored_sleep_calendar_into_sleep() -> None:
    """수면 금지 때문에 되살아난 수면 일정이 틀 문장으로 남고 체류와 겹쳤다(#134).

    틀 문장을 `SLEEP` 으로 고쳐 쓰는 것은 프롬프트가 맡고, 수면과 겹친 event 를 지우는 것은
    코드가 맡는다(`remove_events_overlapping_sleep`). 프롬프트로는 안정적으로 지우지 못했다.
    """

    text = _repair_v3()
    section = _between(text, "### 2단계.", "### 3단계.")

    assert "event를 만들지 않습니다" not in text
    assert "수면 일정이면 `SLEEP`" in section
    assert "`캘린더에 적어 둔 일정이다`로 남기지 않습니다" in section
    # 겹친 event 는 코드가 지운다(`remove_events_overlapping_sleep`). 프롬프트가 다시 시키지 않는다.
    assert "수면과 겹친 다른 event는 코드가 지웁니다" in section


def test_repair_v3_allows_home_without_a_rule() -> None:
    """Question 은 같은 사건을 `집` 이라 불렀는데 Timeline·Repair 는 아파트 이름을 썼다(#134).

    v2 Repair 에는 집 규칙이 없었다. 하루 전체를 먼저 한 문장으로 정리하게 하자 모델이 스스로
    아파트 체류를 집으로 읽었다. 그래서 집을 붙이라는 규칙을 두지 않고, 하루 요약과 막던
    제약을 푸는 허용만 둔다. 규칙을 덧붙였을 때는 효과 없이 프롬프트만 늘었다(live).
    """

    text = _repair_v3()
    order = _between(text, "## 작업 순서", "### 1단계.")
    assert "이 사람은 오늘 어떤 하루를 보냈는가?" in order
    assert "집입니다" not in order
    assert "집도 흐름으로 추론합니다" not in text
    # 같은 곳을 집으로 부르는 것을 "장소를 바꾸지 않는다"·"이름을 뭉개지 않는다" 가 막지 않는다.
    memory = _between(text, "#### User Memory 반영", "### 4단계.")
    assert "그렇게 부를 때까지 기다리지 않고" in memory
    assert "같은 곳의 다른 이름입니다" in _between(text, "할 수 없는 것:", "### 4단계.")
    blur = _between(
        text, "#### Timeline이 쓴 구체적인 이름은 뭉개지 않습니다", "#### 말투와 길이"
    )
    assert "`집`·`학교`·`회사`로 부르는 것은 뭉개는 것이 아닙니다" in blur


def test_repair_v3_fills_activities_only_as_far_as_the_evidence_goes() -> None:
    section = _between(_repair_v3(), "### 3단계.", "#### 합리적인 추론")

    assert "같은 뜻의 다른 문장으로 바꾸는 것은 채운 것이 아닙니다" in section
    assert "| 고치기 전 | 근거 | 고친 뒤 |" in section
    assert "행동으로 바꾸지 않습니다" in section


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

    assert "일정 시간을 따르고, 일정이 없으면 체류 구간을 따릅니다" in time_line


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


# --- 위치만 있는 event 와 문장 (#138) ----------------------------------------------


def test_repair_v3_decides_whether_a_stay_card_is_the_same_visit() -> None:
    """같은 방문인지는 의미 판단이라 Repair 가 정한다. 일정·사진은 남고 빈칸도 없다."""

    section = _between(
        _repair_v3(), "#### `LOCATION_ONLY_OVERLAP`", "#### `STAY_WORD_IN_TITLE`"
    )

    assert "`absorb_location_event`로 그 event에 흡수합니다" in section
    assert "흡수하지 않고 둡니다" in section
    assert "빈칸이 생깁니다" in section
    assert "일정·사진 event끼리는 합치지 않습니다" in section
    for example in ("`치과 검진`", "`회사` 체류"):
        assert example in section


def test_repair_v3_keeps_stay_words_times_and_addresses_out_of_titles() -> None:
    text = _repair_v3()
    section = _between(text, "#### 문장에 쓰지 않는 것", "#### 예시")
    examples = _between(text, "#### 예시", "## warning을 읽는 법")

    assert "`재체류`" in section
    assert "`집에서 보낸 밤`" in section
    assert "`자정 전`" in section
    assert "하루 중 때를 가리키는 말은 쓸 수 있습니다" in section
    assert "장소명에 들어 있는 숫자는 그대로 둡니다" in section
    assert "`자정 전 귀가` → `집으로 귀가`" in examples
    # 같은 규칙은 한 곳에만 둔다.
    assert text.count("`집에서 보낸 밤`처럼") == 1

# --- 장소 성격으로 읽는 활동과 장소 후보 (#140) ------------------------------------


def test_repair_v3_reads_place_activities_by_the_same_table_as_timeline() -> None:
    """Repair 는 Timeline 과 같은 기준으로 누락만 채운다. 표가 갈리면 서로 되돌린다."""

    from tests.agents.test_timeline_v3_prompt import place_activity_table

    repair = place_activity_table(_repair_v3())

    assert repair
    assert repair == place_activity_table(_timeline_v3())


def test_repair_v3_fills_only_missing_place_activities() -> None:
    section = _between(_repair_v3(), "#### 장소 성격으로 읽는 활동", "#### 장소 후보 다시 고르기")

    assert "고치는 것은 둘뿐입니다" in section
    assert "장소 성격으로 쓴 활동은 되돌리지 않습니다" in section
    assert "`데이트`라고 쓰지 않습니다" in section
    assert "이 단계에서 다시 추론하지 않습니다" in _between(_repair_v3(), "### 3단계.", "#### 합리적인 추론")


def test_repair_v3_can_pick_another_place_candidate() -> None:
    text = _repair_v3()
    section = _between(text, "#### 장소 후보 다시 고르기", "#### User Memory 반영")
    inputs = _between(text, "## 입력 의미", "## 작업 순서")

    assert "`places`" in inputs and "`address`" in inputs
    assert "후보 목록은 고치지 않습니다" in section
    assert "`update_event` 한 번에 `place`와 함께" in section
    assert "`sourceRefs`에 넣습니다" in section
    assert "가게를 골라 그곳에서 한 일처럼 쓰지 않습니다" in section
    assert "`PLACE_MISMATCH`" in _between(text, "## 문제 분류", "## 도구 선택")
