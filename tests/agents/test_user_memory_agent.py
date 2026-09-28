"""User Memory 갱신 Agent (#64, #121).

무엇을 근거로 읽는지는 **프롬프트 세트가 정한다.**

- v1·v2 는 AI 가 쓴 문장에서 사용자 성향을 뽑지 않는다. 그 규칙이 프롬프트에서
  사라지면 모델은 `title` 과 `subtitle` 에서 성격을 만들어 내고, 그 프로필이 다시 다음
  타임라인 문장을 만드는 데 쓰여 스스로를 강화한다.
- v3 는 AI 가 쓴 문장도 근거로 읽고, 폭넓게 모으고, 한 번 나온 정보도 남긴다(#121).

두 정책이 섞이면 어느 쪽도 지켜지지 않는다. 그래서 세트마다 자기 문구를 갖는지,
상대의 문구가 남아 있지 않은지를 함께 본다.
"""

import json
import re
from pathlib import Path

import pytest

from app.agents.user_memory import UserMemoryAgent, build_update_prompt
from app.agents.user_memory import user_memory_agent
from app.schemas.user_memory import (
    CUSTOM_ATTRIBUTE_MAX_LENGTH,
    METADATA_FIELDS,
    NARRATIVE_MAX_LENGTH,
    UserMemory,
)
from app.services.user_memory_limits import (
    USER_MEMORY_MAX_CHARS,
    USER_MEMORY_TARGET_CHARS,
    build_daily_timeline_digest,
    serialized_chars,
)
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


def _digest(events=None, *, emotion_type=None):
    payload = [
        daily_timeline(
            emotion_type=emotion_type,
            events=events if events is not None else [daily_timeline_event()],
        )
    ]
    return build_daily_timeline_digest([DailyTimeline.model_validate(item) for item in payload])


@pytest.fixture
def memo_only_traits(monkeypatch: pytest.MonkeyPatch):
    """성향 근거를 `memo` 로 제한하는 세트(v1·v2)로 조립한다.

    분기값은 모듈 로드 시점에 `PROMPT_VERSION` 으로 정해진다. 그대로 두면 이 테스트의
    결과가 실행 환경의 `.env` 를 따라간다. 버전에서 분기값이 정해지는 것 자체는
    `test_prompt_version_graph.py` 가 본다.
    """

    monkeypatch.setattr(user_memory_agent, "_MEMO_ONLY_TRAITS", True)


@pytest.fixture
def ai_sentences_as_evidence(monkeypatch: pytest.MonkeyPatch):
    """AI 가 쓴 문장도 근거로 읽는 세트(v3)로 조립한다."""

    monkeypatch.setattr(user_memory_agent, "_MEMO_ONLY_TRAITS", False)


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


def test_memo_only_set_is_told_when_a_day_has_no_memo(memo_only_traits):
    """빈 자리를 메우려는 것을 막는다. 알려 주지 않으면 AI 문장에서 성향을 만든다."""

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo=None)]))

    assert "[근거 없음]" in prompt
    assert "personality" in prompt
    assert "기존 값을 그대로" in prompt


def test_memo_only_set_gets_no_such_hint_when_there_is_a_memo(memo_only_traits):
    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="오늘은 좋았어요.")]))

    assert "[근거 없음]" not in prompt
    assert "오늘은 좋았어요." in prompt


def test_v3_is_never_told_to_leave_traits_untouched(ai_sentences_as_evidence):
    """v3 는 `memo` 없는 날에도 갱신한다(#121).

    "성향 필드는 그대로 두라" 는 지시가 user prompt 에 남으면 시스템 프롬프트와 정면으로
    어긋나고, 모델은 둘 중 뒤에 온 쪽을 따르기 쉽다.
    """

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo=None)]))

    assert "[근거 없음]" not in prompt
    assert "기존 값을 그대로" not in prompt


@pytest.mark.parametrize("fixture_name", ["memo_only_traits", "ai_sentences_as_evidence"])
def test_request_asks_for_the_whole_document_without_stating_a_policy(
    fixture_name: str, request: pytest.FixtureRequest
):
    """출력이 문서 전체라는 계약은 세트를 가리지 않는다.

    무엇을 남기고 버릴지는 시스템 프롬프트의 몫이라 여기서 말하지 않는다. 예전 문장은
    "압축·삭제" 를 지시했는데, 한 번 나온 정보도 남기는 v3 와 어긋난다.
    """

    request.getfixturevalue(fixture_name)

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="메모")]))

    assert "User Memory 전체" in prompt
    assert "전체 갱신본" in prompt
    assert "압축" not in prompt
    assert "삭제" not in prompt


# --- 크기 (#121) --------------------------------------------------------


def _profile_over_the_target() -> UserMemory:
    sentences = " ".join(f"문장 {index}번입니다." for index in range(40))
    return UserMemory(
        basic_profile=sentences[:NARRATIVE_MAX_LENGTH],
        life_context=sentences[:NARRATIVE_MAX_LENGTH],
        relationships=sentences[:NARRATIVE_MAX_LENGTH],
        personality=sentences[:NARRATIVE_MAX_LENGTH],
    )


def test_prompt_tells_the_size_of_the_existing_profile():
    """모델은 글자 수를 세지 못한다. 알려 주지 않으면 상한에 닿은 줄 모르고 얹는다."""

    existing = UserMemory(basic_profile="30대 개발자입니다.")

    prompt = build_update_prompt(existing, _digest())

    assert "[크기]" in prompt
    assert f"기존 프로필은 {serialized_chars(existing)}자입니다" in prompt
    assert f"목표 크기는 {USER_MEMORY_TARGET_CHARS}자" in prompt
    assert f"{USER_MEMORY_MAX_CHARS}자를 넘으면 저장되지 않습니다" in prompt


@pytest.mark.parametrize("existing", [None, UserMemory()])
def test_prompt_has_no_size_section_without_a_profile(existing):
    assert "[크기]" not in build_update_prompt(existing, _digest())


def test_small_profile_gets_no_shrink_budget():
    prompt = build_update_prompt(UserMemory(basic_profile="30대 개발자입니다."), _digest())

    assert "먼저 줄이세요" not in prompt
    assert "문장 →" not in prompt


def test_profile_over_the_target_gets_a_sentence_budget_before_new_information():
    """상한에 닿은 프로필은 매일 이 경로를 지난다.

    먼저 줄이고 나서 얹어야 1차 출력이 상한 안에 든다. 몫은 문장 수로 준다 — 모델이
    따르는 단위가 그것이다.
    """

    existing = _profile_over_the_target()

    prompt = build_update_prompt(existing, _digest())

    assert serialized_chars(existing) > USER_MEMORY_TARGET_CHARS
    assert "기존 프로필이 이미 목표를" in prompt
    assert "먼저 줄이세요" in prompt
    assert "  - `basicProfile`: 지금 " in prompt
    assert "  - `personality`: 지금 " in prompt


def test_size_section_carries_numbers_not_a_policy():
    """무엇을 줄일지는 시스템 프롬프트의 정책이고 세트마다 다르다."""

    prompt = build_update_prompt(_profile_over_the_target(), _digest())
    section = prompt.split("[크기]")[1].split("\n\n")[0]

    for policy_word in ("제거", "병합", "중복", "오래된"):
        assert policy_word not in section


def test_retry_with_the_previous_output_asks_to_fix_it_not_to_start_over():
    """재요청이 직전 출력에서 이어 가야 시도마다 줄어든다(#121)."""

    previous = UserMemory(basic_profile="직전에 낸 문서입니다.")

    prompt = build_update_prompt(
        None,
        _digest(),
        violations=["전체 크기가 상한을 넘었습니다."],
        previous=previous,
    )

    assert "[직전 출력]" in prompt
    assert "직전에 낸 문서입니다." in prompt
    assert "직전 출력을 고쳐" in prompt
    assert "처음부터 다시 만들지 말고" in prompt
    assert "User Memory 전체" in prompt
    # 두 지시가 함께 나가면 모델은 뒤에 온 쪽을 따른다.
    assert "다시 만드세요" not in prompt
    assert prompt.index("[직전 출력]") < prompt.index("[직전 출력이 규칙을 어겼습니다]")


def test_previous_output_is_ignored_without_a_violation():
    """고칠 이유가 없으면 고칠 문서도 싣지 않는다."""

    prompt = build_update_prompt(
        None, _digest(), previous=UserMemory(basic_profile="직전에 낸 문서입니다.")
    )

    assert "[직전 출력]" not in prompt
    assert "직전에 낸 문서입니다." not in prompt


def test_agent_passes_the_previous_output_to_the_model():
    llm = FakeLLM([memory_json()])

    UserMemoryAgent(llm=llm).generate(
        None,
        _digest(),
        violations=["전체 크기가 상한을 넘었습니다."],
        previous=UserMemory(basic_profile="직전에 낸 문서입니다."),
    )

    assert "직전에 낸 문서입니다." in llm.calls[0].prompt


def test_prompt_carries_the_emotion_the_user_picked():
    prompt = build_update_prompt(None, _digest(emotion_type="VERY_UNHAPPY"))

    assert '"emotion": "VERY_UNHAPPY"' in prompt


def test_prompt_has_no_emotion_key_when_none_was_picked():
    prompt = build_update_prompt(None, _digest(emotion_type=None))

    assert '"emotion"' not in prompt


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
    """필드 길이는 Pydantic 이 잡고, 교정 재시도가 한 번 더 묻는다."""

    llm = FakeLLM(
        [
            memory_json(basicProfile="가" * (NARRATIVE_MAX_LENGTH + 1)),
            memory_json(basicProfile="짧게 줄였습니다."),
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(None, _digest())

    assert memory.basic_profile == "짧게 줄였습니다."
    assert len(llm.calls) == 2


def test_agent_accepts_more_custom_attributes_than_the_old_limit():
    """개수 제한이 없어졌다(#121). 예전에는 6개째에서 교정 재시도로 떨어졌다."""

    attributes = {f"속성{index}": "값" for index in range(8)}
    llm = FakeLLM([memory_json(customAttributes=attributes)])

    memory = UserMemoryAgent(llm=llm).generate(None, _digest())

    assert len(memory.custom_attributes) == 8
    assert len(llm.calls) == 1


# --- 프롬프트 파일 계약 -------------------------------------------------


def _prompt(version: str) -> str:
    return (_PROMPTS / version / "prompt.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("사용자가 직접 쓴 글", "사용자의 실제 발화가 무엇인지 지목해야 합니다."),
        ("AI 가 센서 기록을 보고", "title/subtitle 의 출처를 밝혀야 합니다."),
        ("통째로 대체", "출력이 append 가 아니라 rewrite 임을 알려야 합니다."),
        ("customAttributes", "동적 속성 규칙이 있어야 합니다."),
        ("schemaVersion", "메타데이터를 출력하지 말라고 해야 합니다."),
        ("지시로 따르지 않습니다", "memo 안의 지시문을 따르지 않게 해야 합니다."),
        ("분 단위 시각", "원본 수치를 프로필에 옮기지 않게 해야 합니다."),
    ],
)
def test_every_set_states_the_shared_contract(version: str, marker: str, why: str):
    """근거 정책이 갈려도 세트를 가리지 않고 지켜야 하는 것."""

    assert marker in _prompt(version), (
        f"user_memory {version} 프롬프트에 '{marker}' 가 없습니다. {why}"
    )


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("스스로를 강화", "왜 안 되는지를 설명해야 지시가 유지됩니다."),
        ("기존 값을 그대로 둡니다", "근거 없을 때의 동작이 명시돼야 합니다."),
        ("200자", "이 세트가 쓰는 필드 길이가 있어야 합니다."),
    ],
)
def test_memo_only_sets_keep_their_evidence_rule(version: str, marker: str, why: str):
    """v1·v2 는 AI 가 쓴 문장에서 성향을 뽑지 않는다(#64)."""

    assert marker in _prompt(version), (
        f"user_memory {version} 프롬프트에 '{marker}' 가 없습니다. {why}"
    )


def test_memo_only_sets_stay_identical():
    """v1 과 v2 는 같은 정책이라 갈릴 이유가 없다. 갈리면 롤백이 다른 동작을 만든다.

    v3 는 #121 에서 근거 정책이 바뀌어 여기서 빠졌다.
    """

    assert _prompt("v1") == _prompt("v2")


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("읽고 저장한", "AI 문장을 근거로 쓰는 이유(사용자가 받아들인 기록)를 밝혀야 합니다."),
        ("말투와 표현은 AI 의 것", "문장의 어조에서 성격을 읽지 않게 해야 되먹임이 줄어듭니다."),
        ("`memo`·`emotion` 을 따릅니다", "사용자가 직접 남긴 것이 이겨야 합니다."),
        ("`memo` 가 없는 날에도 갱신합니다", "메모 없는 날의 동작이 명시돼야 합니다."),
        ("폭넓게", "수집 범위를 넓힌다고 적어야 합니다."),
        ("거르지 않습니다", "타임라인 쓸모로 거르지 않는다고 적어야 합니다."),
        ("사는 곳, 나이, 성별, 직업, 신분", "이슈가 꼽은 수집 대상이 있어야 합니다."),
        ("만나거나 대화하는 사람", "이슈가 꼽은 수집 대상이 있어야 합니다."),
        ("적극적으로 추론합니다", "추론을 하라고 적어야 합니다."),
        ("`~로 보입니다`", "추론과 확인된 사실을 구분해 적게 해야 합니다."),
        ("반복되는지는 남기는 조건이 아닙니다", "한 번 나온 정보를 남기는 규칙이 있어야 합니다."),
        ("반복으로 고쳐 씁니다", "다시 나온 정보를 반복으로 올리는 규칙이 있어야 합니다."),
        ("겹치면 합칩니다", "병합 규칙이 있어야 합니다."),
        ("새로운 내용은 더합니다", "추가 규칙이 있어야 합니다."),
        ("충돌하면 새 정보로 바꿉니다", "충돌 규칙이 있어야 합니다."),
        ("개수 제한은 없습니다", "customAttributes 개수 제한이 없다고 적어야 합니다."),
        ("정보를 지우는 것은 마지막입니다", "줄이는 순서가 보존 정책을 따라야 합니다."),
        ("`[REDACTED_…]`", "가린 자리를 프로필에 옮기지 않게 해야 합니다."),
    ],
)
def test_v3_states_the_broad_collection_policy(marker: str, why: str):
    assert marker in _prompt("v3"), f"user_memory v3 프롬프트에 '{marker}' 가 없습니다. {why}"


@pytest.mark.parametrize(
    ("removed", "why"),
    [
        ("스스로를 강화", "AI 문장을 근거로 쓰지 말라는 설명이 남아 있습니다."),
        ("기존 값을 그대로 둡니다", "memo 없는 날 성향 필드를 두라는 지시가 남아 있습니다."),
        ("**`memo` 만.**", "성향 필드의 근거를 memo 로 제한하는 표가 남아 있습니다."),
        ("반복 확인된", "반복을 저장 조건으로 삼는 규칙이 남아 있습니다."),
        ("성향을 단정하지 않습니다", "하루치 기록으로는 적지 말라는 규칙이 남아 있습니다."),
        ("일정 기간 유효한 특성", "일회성 정보를 버리라는 규칙이 남아 있습니다."),
        ("실제로 도움이 되는가", "타임라인 쓸모로 거르는 조건이 남아 있습니다."),
        ("하루짜리 사건", "하루짜리 사건을 금지하는 규칙이 남아 있습니다."),
        ("이름이 아니라 관계로", "사람 이름을 금지하는 규칙이 남아 있습니다."),
        ("200자", "옛 필드 길이가 남아 있습니다."),
        ("150자", "옛 customAttributes 길이가 남아 있습니다."),
        ("최대 5개", "옛 customAttributes 개수가 남아 있습니다."),
        ("짧을수록 좋습니다", "눌러 담으라는 지시가 남아 있습니다."),
    ],
)
def test_v3_drops_the_rules_the_new_policy_replaced(removed: str, why: str):
    """옛 규칙이 한 줄이라도 남으면 모델은 두 정책 사이에서 보수적인 쪽을 고른다."""

    assert removed not in _prompt("v3"), f"user_memory v3 프롬프트: {why}"


def test_v3_states_the_limits_the_code_enforces():
    """프롬프트가 말하는 숫자와 코드가 거절하는 숫자가 같아야 한다.

    다르면 모델은 프롬프트를 지키고도 거절당하거나, 코드가 받아 줄 것을 미리 줄인다.
    """

    text = _prompt("v3")

    assert f"각 자연어 필드: 최대 {NARRATIVE_MAX_LENGTH}자" in text
    assert f"값 하나당 최대 {CUSTOM_ATTRIBUTE_MAX_LENGTH}자" in text
    assert (
        f"전체 프로필: 최대 {USER_MEMORY_MAX_CHARS:,}자, "
        f"목표 {USER_MEMORY_TARGET_CHARS:,}자"
    ) in text


def test_v3_has_the_size_section_the_violation_message_points_to():
    """크기 위반 지적은 줄이는 순서를 말하지 않고 이 절을 가리킨다."""

    text = _prompt("v3")

    assert "\n## 크기\n" in text
    assert "넘칠 것 같으면 이 순서로" in text


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("크기는 남기는 규칙보다 앞섭니다", "보존과 상한이 부딪칠 때 무엇이 이기는지 적어야 합니다."),
        ("먼저 줄여 자리를 만듭니다", "상한에 닿은 프로필에 새 정보를 얹는 순서가 있어야 합니다."),
        ("그 수 이내로 씁니다", "코드가 주는 문장 수 몫을 따르라고 적어야 합니다."),
        ("끝까지 남기는 것", "줄일 때 지키는 사실이 있어야 합니다."),
        ("직전 출력을 고칩니다", "재요청에서 처음부터 다시 만들지 않게 해야 합니다."),
    ],
)
def test_v3_tells_how_to_stay_under_the_cap(marker: str, why: str):
    """보존 정책 아래에서 프로필은 며칠이면 상한에 닿고 그 뒤로는 매일 상한 근처다.

    그 구간의 규칙이 없으면 갱신이 매일 실패한다(실측에서 닷새 중 나흘).
    """

    assert marker in _prompt("v3"), f"user_memory v3 프롬프트에 '{marker}' 가 없습니다. {why}"


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_memo_only_sets_have_the_size_section_too(version: str):
    text = _prompt(version)

    assert "\n## 크기\n" in text
    assert "넘칠 것 같으면 이 순서로 줄입니다" in text


def test_v3_explains_every_emotion_the_app_server_sends():
    """App Server 의 `EmotionType` 다섯 값. 뜻을 모르는 값은 모델이 짐작한다."""

    text = _prompt("v3")

    for value in ("VERY_HAPPY", "HAPPY", "NEUTRAL", "UNHAPPY", "VERY_UNHAPPY"):
        assert f"- `{value}`: " in text, f"user_memory v3 에 감정 `{value}` 의 뜻이 없습니다."
    assert "직접 고른 감정" in text
    assert "그날 있던 일과 함께 읽습니다" in text
    assert "뜻을 짐작하지 않고 쓰지 않습니다" in text


def test_v3_names_every_key_the_digest_carries():
    """프롬프트가 설명하는 입력 키가 코드가 싣는 키와 맞아야 한다.

    코드에서 키를 더하고 프롬프트를 두면 모델은 뜻을 모르는 값을 받는다.
    """

    digest = _digest(
        [daily_timeline_event(subtitle="동료들과 함께였어요", memo="편했다")],
        emotion_type="HAPPY",
    )
    entry = digest.daily_timelines[0]
    keys = set(entry) | set(entry["events"][0])
    text = _prompt("v3")

    for key in sorted(keys):
        assert f"`{key}`" in text, f"user_memory v3 에 입력 키 `{key}` 설명이 없습니다."


def test_v3_keeps_the_output_example_in_step_with_the_schema():
    """출력 예시의 키가 스키마와 다르면 모델은 예시를 계약으로 알고 채운다."""

    block = re.search(r"```json\n(.*?)```", _prompt("v3"), re.S)
    assert block, "user_memory v3 프롬프트에 JSON 출력 예시가 없습니다."

    declared = {
        field.alias or name for name, field in UserMemory.model_fields.items()
    }
    assert set(json.loads(block.group(1))) == declared - set(METADATA_FIELDS)
