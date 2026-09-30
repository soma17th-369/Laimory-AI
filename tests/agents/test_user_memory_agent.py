"""User Memory 갱신 Agent (#64, #121).

무엇을 근거로 읽는지는 **프롬프트 세트가 정한다.**

- v1·v2 는 AI 가 쓴 문장에서 사용자 성향을 뽑지 않는다. 그 규칙이 프롬프트에서
  사라지면 모델은 `title` 과 `subtitle` 에서 성격을 만들어 내고, 그 프로필이 다시 다음
  타임라인 문장을 만드는 데 쓰여 스스로를 강화한다.
- v3 는 AI 가 쓴 문장도 근거로 읽고, 폭넓게 모으고, 한 번 나온 정보도 남긴다(#121).

두 정책이 섞이면 어느 쪽도 지켜지지 않는다. 그래서 세트마다 자기 문구를 갖는지,
상대의 문구가 남아 있지 않은지를 함께 본다.

모델이 **무엇을 출력하는지**도 세트마다 다르다(#121). v1·v2 는 문서 전체를 다시 쓰고,
v3 는 변경 목록(어느 항목을 추가·수정·삭제할지)을 낸다. 코드가 그것을 기존 문서에 끼워
넣으므로, 목록에 없는 항목은 글자 하나 바뀌지 않아야 한다.

v3 프롬프트는 **작업 단계 순서대로** 읽히고, 항목 하나를 채우는 데 필요한 것(담는 것·근거·
추론·갱신·근거가 약할 때·예시)이 그 항목의 절 하나에 모여 있어야 한다.
"""

import json
import re
from pathlib import Path

import pytest

from app.agents.user_memory import UserMemoryAgent, build_update_prompt
from app.agents.user_memory import user_memory_agent
from app.schemas.user_memory import (
    CUSTOM_ATTRIBUTE_ITEM_PREFIX,
    CUSTOM_ATTRIBUTE_MAX_LENGTH,
    METADATA_FIELDS,
    NARRATIVE_MAX_LENGTH,
    UserMemory,
    UserMemoryChange,
    UserMemoryChangeAction,
    UserMemoryPatch,
)
from app.services.user_memory_limits import (
    USER_MEMORY_MAX_CHARS,
    USER_MEMORY_TARGET_CHARS,
    build_daily_timeline_digest,
    serialized_chars,
)
from app.schemas.user_memory_update import DailyTimeline
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.user_memory import (
    NARRATIVE_FIELDS,
    change,
    changes_json,
    daily_timeline,
    daily_timeline_event,
    memory_json,
)

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
def legacy_set(monkeypatch: pytest.MonkeyPatch):
    """v1·v2 세트로 돈다 — 성향 근거는 `memo` 뿐이고 모델이 문서 전체를 다시 쓴다.

    분기값은 모듈 로드 시점에 `PROMPT_VERSION` 으로 정해진다. 그대로 두면 이 테스트의
    결과가 실행 환경의 `.env` 를 따라간다. 버전에서 분기값이 정해지는 것 자체는
    `test_prompt_version_graph.py` 가 본다.
    """

    monkeypatch.setattr(user_memory_agent, "_MEMO_ONLY_TRAITS", True)
    monkeypatch.setattr(user_memory_agent, "_PATCH_OUTPUT", False)


@pytest.fixture
def v3_set(monkeypatch: pytest.MonkeyPatch):
    """v3 세트로 돈다 — AI 가 쓴 문장도 근거로 읽고 모델이 변경 목록을 낸다."""

    monkeypatch.setattr(user_memory_agent, "_MEMO_ONLY_TRAITS", False)
    monkeypatch.setattr(user_memory_agent, "_PATCH_OUTPUT", True)


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


def test_memo_only_set_is_told_when_a_day_has_no_memo(legacy_set):
    """빈 자리를 메우려는 것을 막는다. 알려 주지 않으면 AI 문장에서 성향을 만든다."""

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo=None)]))

    assert "[근거 없음]" in prompt
    assert "personality" in prompt
    assert "기존 값을 그대로" in prompt


def test_memo_only_set_gets_no_such_hint_when_there_is_a_memo(legacy_set):
    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="오늘은 좋았어요.")]))

    assert "[근거 없음]" not in prompt
    assert "오늘은 좋았어요." in prompt


def test_v3_is_never_told_to_leave_traits_untouched(v3_set):
    """v3 는 `memo` 없는 날에도 갱신한다(#121).

    "성향 필드는 그대로 두라" 는 지시가 user prompt 에 남으면 시스템 프롬프트와 정면으로
    어긋나고, 모델은 둘 중 뒤에 온 쪽을 따르기 쉽다.
    """

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo=None)]))

    assert "[근거 없음]" not in prompt
    assert "기존 값을 그대로" not in prompt


def test_legacy_request_asks_for_the_whole_document(legacy_set):
    """v1·v2 는 문서 전체를 다시 쓴다. 그 프롬프트가 그렇게 적혀 있다."""

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="메모")]))

    assert "User Memory 전체" in prompt
    assert "전체 갱신본" in prompt
    assert "바꿀 항목만" not in prompt


def test_v3_request_asks_only_for_the_items_to_change(v3_set):
    """v3 는 변경 목록을 낸다(#121).

    전체를 다시 쓰게 하면 건드릴 이유가 없던 항목까지 조금씩 달라지거나 빠진다. 문서
    모양에 바꾸지 않는 필드만 null 로 두게 했을 때도 모델은 그것을 "새 문서" 로 읽었다.
    """

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="메모")]))
    request = prompt.rsplit("\n\n", 1)[1]

    assert "바꿀 항목만" in request
    assert "`changes`" in request
    assert request.index("`reason`") < request.index("`text`")
    assert "`text` 에는 그 항목의 전체 문장" in request
    assert "바꾸지 않는 항목은 적지 않습니다" in request
    assert "빈 목록" in request
    assert "null" not in request
    assert "User Memory 전체" not in prompt
    assert "전체 갱신본" not in prompt


@pytest.mark.parametrize("fixture_name", ["legacy_set", "v3_set"])
def test_request_states_the_shape_of_the_output_not_a_policy(
    fixture_name: str, request: pytest.FixtureRequest
):
    """무엇을 남기고 버릴지는 시스템 프롬프트의 몫이라 여기서 말하지 않는다.

    예전 문장은 "압축·삭제" 를 지시했는데, 한 번 나온 정보도 남기는 v3 와 어긋난다.
    """

    request.getfixturevalue(fixture_name)

    prompt = build_update_prompt(None, _digest([daily_timeline_event(memo="메모")]))

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


def test_retry_with_the_previous_output_asks_to_fix_it_not_to_start_over(legacy_set):
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


def test_v3_retry_asks_only_for_the_items_to_change_in_the_previous_output(v3_set):
    prompt = build_update_prompt(
        None,
        _digest(),
        violations=["전체 크기가 상한을 넘었습니다."],
        previous=UserMemory(basic_profile="직전에 낸 문서입니다."),
    )

    assert "[직전 출력]" in prompt
    assert "직전 출력에서 바꿀 항목만" in prompt
    assert "`changes`" in prompt
    assert "나머지 항목은 적지 않습니다" in prompt
    assert "처음부터 다시 만들지 말고" in prompt
    assert "User Memory 전체" not in prompt


def test_previous_output_is_ignored_without_a_violation():
    """고칠 이유가 없으면 고칠 문서도 싣지 않는다."""

    prompt = build_update_prompt(
        None, _digest(), previous=UserMemory(basic_profile="직전에 낸 문서입니다.")
    )

    assert "[직전 출력]" not in prompt
    assert "직전에 낸 문서입니다." not in prompt


def test_agent_passes_the_previous_output_to_the_model(legacy_set):
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


def test_agent_returns_a_validated_memory(legacy_set):
    agent = UserMemoryAgent(llm=FakeLLM([memory_json(basicProfile="30대 개발자입니다.")]))

    memory = agent.generate(None, _digest())

    assert memory.basic_profile == "30대 개발자입니다."


def test_agent_sends_the_system_prompt(legacy_set):
    llm = FakeLLM([memory_json()])

    UserMemoryAgent(llm=llm).generate(None, _digest())

    assert "User Memory 갱신 시스템 프롬프트" in llm.calls[0].system


def test_over_length_field_is_repaired_by_the_structured_path(legacy_set):
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


def test_agent_accepts_more_custom_attributes_than_the_old_limit(legacy_set):
    """개수 제한이 없어졌다(#121). 예전에는 6개째에서 교정 재시도로 떨어졌다."""

    attributes = {f"속성{index}": "값" for index in range(8)}
    llm = FakeLLM([memory_json(customAttributes=attributes)])

    memory = UserMemoryAgent(llm=llm).generate(None, _digest())

    assert len(memory.custom_attributes) == 8
    assert len(llm.calls) == 1


# --- 부분 갱신 (#121, v3) -----------------------------------------------


def _existing_profile() -> UserMemory:
    return UserMemory(
        basic_profile="망원동에 사는 30대 개발자입니다.",
        relationships="김민수: 같은 팀 동료.",
        routines="평일에는 회사에서 일합니다.",
        custom_attributes={
            "반려동물": "고양이 한 마리를 키웁니다.",
            "여행": "8월 말에 강릉에 다녀왔습니다.",
        },
    )


def test_v3_changes_only_the_items_the_model_returned(v3_set):
    """변경 목록에 없는 항목은 글자 하나 바뀌지 않는다."""

    existing = _existing_profile()
    llm = FakeLLM(
        [
            changes_json(
                change("routines", "수정", "평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다.")
            )
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(existing, _digest())

    assert memory.routines == "평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다."
    assert memory.basic_profile == existing.basic_profile
    assert memory.relationships == existing.relationships
    assert memory.custom_attributes == existing.custom_attributes
    assert len(llm.calls) == 1


def test_v3_adds_and_replaces_custom_attributes(v3_set):
    existing = _existing_profile()
    llm = FakeLLM(
        [
            changes_json(
                change("customAttributes.운동", "추가", "합정 클라이밍장을 다닙니다."),
                change("customAttributes.반려동물", "수정", "고양이 두 마리를 키웁니다."),
            )
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(existing, _digest())

    assert memory.custom_attributes == {
        "반려동물": "고양이 두 마리를 키웁니다.",
        "여행": "8월 말에 강릉에 다녀왔습니다.",
        "운동": "합정 클라이밍장을 다닙니다.",
    }
    assert memory.basic_profile == existing.basic_profile


# --- 기존 내용은 지우지 않는다 (#121) -----------------------------------
#
# 달라졌으면 고쳐 쓰는 것이고, 이번 기록에 나오지 않았으면 그대로 두는 것이다. 실제 모델은
# "이번 기록에 기타 이야기가 없다" 는 이유로 있던 속성을 지웠다. 프롬프트가 금지해도
# 어기므로 코드가 적용하지 않는다.


def test_v3_does_not_apply_a_removal(v3_set):
    existing = _existing_profile()
    llm = FakeLLM(
        [
            changes_json(
                change("customAttributes.여행", "삭제"),
                change("relationships", "삭제"),
                change("routines", "수정", "평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다."),
            )
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(existing, _digest())

    assert memory.custom_attributes == existing.custom_attributes
    assert memory.relationships == existing.relationships
    # 같은 목록의 다른 변경은 그대로 적용한다.
    assert memory.routines == "평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다."
    assert len(llm.calls) == 1


def test_v3_does_not_apply_an_update_that_only_drops_sentences(v3_set):
    """`수정` 으로 기존 문장 몇 개를 빼기만 한 것도 지우는 것이다."""

    existing = UserMemory(
        routines="평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다. 저녁에 산책합니다."
    )
    llm = FakeLLM([changes_json(change("routines", "수정", "평일에는 회사에서 일합니다."))])

    memory = UserMemoryAgent(llm=llm).generate(existing, _digest())

    assert memory.routines == existing.routines


def test_v3_applies_an_update_that_rewrites_what_changed(v3_set):
    """달라져서 바뀌는 것은 막지 않는다. 옛 직장이 새 직장으로 바뀌는 것이 그렇다."""

    existing = UserMemory(basic_profile="판교 회사에서 일합니다. 망원동에 삽니다.")
    llm = FakeLLM(
        [changes_json(change("basicProfile", "수정", "강남 회사에서 일합니다. 망원동에 삽니다."))]
    )

    memory = UserMemoryAgent(llm=llm).generate(existing, _digest())

    assert memory.basic_profile == "강남 회사에서 일합니다. 망원동에 삽니다."


def test_v3_shortens_an_item_the_size_budget_names(v3_set):
    """기존 문서가 목표 크기를 넘었을 때는 줄여야 한다. 그때도 막으면 문서가 상한에 닿은
    뒤로 갱신이 매번 1304 로 끝난다. 다만 줄일 몫을 받은 항목만이다."""

    existing = _profile_over_the_target().model_copy(
        update={"custom_attributes": {"여행": "8월 말에 강릉에 다녀왔습니다."}}
    )
    assert serialized_chars(existing) > USER_MEMORY_TARGET_CHARS
    llm = FakeLLM(
        [
            changes_json(
                change("customAttributes.여행", "삭제"),
                change("personality", "수정", "문장 0번입니다."),
            )
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(existing, _digest())

    assert memory.personality == "문장 0번입니다."
    # 몫을 받지 않은 속성은 목표를 넘은 날에도 그대로다.
    assert memory.custom_attributes == {"여행": "8월 말에 강릉에 다녀왔습니다."}


def test_v3_logs_how_many_removals_it_dropped_without_naming_them(v3_set, caplog):
    existing = _existing_profile()
    llm = FakeLLM([changes_json(change("customAttributes.여행", "삭제"))])

    with caplog.at_level("INFO"):
        UserMemoryAgent(llm=llm).generate(existing, _digest())

    records = [r for r in caplog.records if "지우기만 하는 변경" in r.getMessage()]
    assert len(records) == 1
    dumped = json.dumps(records[0].__dict__, ensure_ascii=False, default=str)
    assert "droppedRemovalCount" in dumped
    assert "여행" not in dumped and "강릉" not in dumped


def test_v3_empty_change_list_returns_the_same_profile(v3_set):
    """이번 기록이 말해 주는 것이 없으면 아무것도 바뀌지 않는다. 실패가 아니다."""

    existing = _existing_profile()

    memory = UserMemoryAgent(llm=FakeLLM([changes_json()])).generate(existing, _digest())

    assert memory.prompt_payload() == existing.prompt_payload()


def test_v3_fills_an_empty_profile_from_a_change_list(v3_set):
    """기존 문서가 없어도(`null`) 빈 문서에 끼워 넣는다. 따로 가르는 경로가 없다."""

    llm = FakeLLM(
        [changes_json(change("basicProfile", "추가", "판교 회사에 다니는 직장인으로 보입니다."))]
    )

    memory = UserMemoryAgent(llm=llm).generate(None, _digest())

    assert memory.prompt_payload() == {"basicProfile": "판교 회사에 다니는 직장인으로 보입니다."}


def test_v3_over_length_item_is_repaired_by_the_structured_path(v3_set):
    """항목 값 하나의 길이 제한은 변경 한 건의 `text` 에 같게 걸린다."""

    llm = FakeLLM(
        [
            changes_json(change("routines", "수정", "가" * (NARRATIVE_MAX_LENGTH + 1))),
            changes_json(change("routines", "수정", "짧게 줄였습니다.")),
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(_existing_profile(), _digest())

    assert memory.routines == "짧게 줄였습니다."
    assert len(llm.calls) == 2


def test_v3_change_to_an_unknown_item_is_repaired_by_the_structured_path(v3_set):
    """없는 항목을 가리킨 변경을 조용히 버리지 않는다. 버리면 모델이 하려던 갱신이 사라진다."""

    llm = FakeLLM(
        [
            changes_json(change("hobbies", "추가", "클라이밍을 합니다.")),
            changes_json(change("customAttributes.취미", "추가", "클라이밍을 합니다.")),
        ]
    )

    memory = UserMemoryAgent(llm=llm).generate(_existing_profile(), _digest())

    assert memory.custom_attributes["취미"] == "클라이밍을 합니다."
    assert len(llm.calls) == 2


def test_v3_retry_applies_the_change_list_to_the_previous_output(v3_set):
    """재요청은 직전 출력을 고친다. 변경 목록도 기존 문서가 아니라 그 문서에 적용한다."""

    existing = _existing_profile()
    previous = existing.model_copy(update={"routines": "직전 시도에서 길게 쓴 문장입니다."})
    llm = FakeLLM([changes_json(change("basicProfile", "수정", "망원동에 사는 개발자입니다."))])

    memory = UserMemoryAgent(llm=llm).generate(
        existing,
        _digest(),
        violations=["전체 크기가 상한을 넘었습니다."],
        previous=previous,
    )

    assert memory.basic_profile == "망원동에 사는 개발자입니다."
    assert memory.routines == "직전 시도에서 길게 쓴 문장입니다."


def test_legacy_set_takes_the_whole_document_as_the_result(legacy_set):
    """v1·v2 는 모델이 낸 문서가 곧 결과다. 출력에 없는 항목은 사라진다."""

    llm = FakeLLM([memory_json(basicProfile="30대 개발자입니다.")])

    memory = UserMemoryAgent(llm=llm).generate(_existing_profile(), _digest())

    assert memory.basic_profile == "30대 개발자입니다."
    assert memory.relationships == ""
    assert memory.custom_attributes == {}


# --- 프롬프트 파일 계약 -------------------------------------------------


def _prompt(version: str) -> str:
    return (_PROMPTS / version / "prompt.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("사용자가 직접 쓴 글", "사용자의 실제 발화가 무엇인지 지목해야 합니다."),
        ("AI 가 센서 기록을 보고", "title/subtitle 의 출처를 밝혀야 합니다."),
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
        ("통째로 대체", "이 세트는 문서 전체를 다시 쓴다고 알려야 합니다."),
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
        ("바꿀 항목만", "문서 전체가 아니라 바꿀 항목만 내라고 적어야 합니다."),
        ("**변경 목록**", "출력이 문서가 아니라 변경 목록이라고 적어야 합니다."),
        ("글자 하나 바뀌지 않고", "목록에 없는 항목이 그대로 남는다고 알려야 건드리지 않습니다."),
        ("그 항목의 전체 문장", "조각만 내면 그 항목의 기존 내용이 사라집니다."),
        ("기존 표현 그대로", "바꾸는 항목 안에서도 바뀌지 않는 문장은 다시 쓰지 않게 해야 합니다."),
        ("뜻이 그대로면 바꾸지 않습니다", "표현만 다듬는 변경을 막아야 합니다."),
        ("기존과 똑같은 키", "키가 다르면 바꾸는 대신 새 속성이 생깁니다."),
        ("줄이는 항목도 변경 목록에 담아야 줄어듭니다", "줄일 항목을 내지 않으면 문서가 상한까지 자랍니다."),
        ("기존 프로필을 쓰지 않습니다", "기록을 프로필로 채워 읽으면 프로필이 자기 내용을 확인해 줍니다."),
        ("하루의 예외는 충돌이 아닙니다", "성향을 하루의 일로 뒤집지 않게 해야 합니다."),
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
        ("통째로 대체", "문서 전체를 다시 쓴다는 설명이 남아 있습니다."),
        ("프로필 전체를 다시 씁니다", "문서 전체를 다시 쓰라는 지시가 남아 있습니다."),
        ("빈 문자열로 둡니다", "바꾸지 않는 필드를 빈 문자열로 내라는 지시가 남아 있습니다."),
        ("`null` 로 둡니다", "바꾸지 않는 필드를 null 로 내는 문서 모양 출력이 남아 있습니다."),
        ("키를 **모두** 포함", "필드를 모두 출력하라는 문서 모양 출력이 남아 있습니다."),
        ('"key"', "속성을 (키, 값) 으로 내는 옛 출력 모양이 남아 있습니다."),
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


def test_v3_output_example_is_a_valid_change_list():
    """출력 예시의 모양이 스키마와 다르면 모델은 예시를 계약으로 알고 채운다.

    v3 의 출력은 문서가 아니라 변경 목록이다. 예시가 그 스키마로 검증되고, 세 동작과
    두 종류의 항목(고정 필드·속성)을 모두 보여 주는지 본다.
    """

    blocks = re.findall(r"```json\n(.*?)```", _prompt("v3"), re.S)
    assert len(blocks) == 1, "user_memory v3 프롬프트의 JSON 출력 예시는 하나여야 합니다."
    example = json.loads(blocks[0])

    assert set(example) == {"changes"}
    assert not set(example) & set(NARRATIVE_FIELDS), "문서 모양 예시가 남아 있습니다."

    patch = UserMemoryPatch.model_validate(example)
    # 선언 순서가 곧 모델이 쓰는 순서다. 이유(reason)가 문장(text)보다 먼저 온다.
    declared = [
        field.alias or name for name, field in UserMemoryChange.model_fields.items()
    ]
    assert all(list(item) == declared for item in example["changes"])
    for item in example["changes"]:
        label, _, fact = item["reason"].partition(": ")
        assert label in _COMPARED and label != "이미 있음", "이유는 `견준 결과: 사실` 입니다."
        assert fact.strip()
    # 예시는 `삭제` 를 보여 주지 않는다. 지우는 것은 크기 때문에 줄일 때뿐이고, 예시에
    # 있으면 모델이 평소에도 지운다. 취소된 계획은 지우지 않고 고쳐 쓰는 것으로 보여 준다.
    assert {change.action for change in patch.changes} == {
        UserMemoryChangeAction.ADD,
        UserMemoryChangeAction.UPDATE,
    }
    assert all(item["text"] for item in example["changes"])
    assert any("취소" in item["text"] for item in example["changes"])
    assert any(change.attribute_key is None for change in patch.changes)
    assert any(change.attribute_key for change in patch.changes)
    for item in example["changes"]:
        assert item["item"] not in METADATA_FIELDS

    items = [item["item"] for item in example["changes"]]
    assert len(items) == len(set(items)), "한 항목은 변경 목록에 한 번만 나옵니다."


def test_v3_defines_the_three_actions_the_schema_accepts():
    """프롬프트가 말하는 동작과 스키마가 받는 동작이 같아야 한다."""

    section = _between(_prompt("v3"), "### `action` 세 가지", "### 한 번 나온 정보도 남깁니다")

    assert re.findall(r"^- `(.+?)`: ", section, re.M) == [
        action.value for action in UserMemoryChangeAction
    ]


def test_v3_names_custom_attribute_items_the_way_the_schema_parses_them():
    """`item` 표기가 코드와 다르면 속성 변경이 전부 거절된다.

    줄일 몫(`shrink_budget`)도 같은 표기로 속성을 가리켜, 모델이 그 이름을 그대로
    `item` 으로 옮겨 적을 수 있다.
    """

    text = _prompt("v3")

    assert f"`{CUSTOM_ATTRIBUTE_ITEM_PREFIX}<키>`" in text
    assert f'"item": "{CUSTOM_ATTRIBUTE_ITEM_PREFIX}' in text


# --- v3: 읽는 순서 (#121) -------------------------------------------------

#: 위에서 아래로 읽는 순서. 작업 단계의 순서와 같다.
_V3_HEADINGS = (
    "## Laimory 공통 제품 비전",
    "## 당신의 역할",
    "## 입력 데이터의 의미",
    "## 전체 작업 흐름",
    "## 1단계. 기록 읽기",
    "## 2단계. 항목별 추론",
    "## 항목별 규칙",
    "## 3단계. 변경 결정",
    "## 4단계. 문장 작성",
    "## 크기",
    "## 남기면 안 되는 것",
    "## 5단계. 최종 검증",
    "## 출력 형식",
)

#: 항목의 절이 빠짐없이 갖는 것. 항목 하나를 채우려고 다른 절을 대조하지 않게 한다.
_ITEM_SECTION_LABELS = (
    "**담는 것**",
    "**읽는 근거**",
    "**추론**",
    "**갱신**",
    "**근거가 약할 때**",
    "**예시**",
    "**피할 문장**",
)

_ITEM_SECTIONS = (*NARRATIVE_FIELDS, "customAttributes")

#: 알게 된 것을 기존 프로필과 견준 결과. `reason` 의 앞머리로 적는다. 코드는 읽지 않고
#: 프롬프트가 정한다.
_COMPARED = ("이미 있음", "새로움", "합침", "반복", "충돌")


def _between(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"{start!r} 가 하나여야 합니다."
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _item_sections() -> dict[str, str]:
    """「항목별 규칙」의 `### \\`항목\\` — …` 절을 항목 이름으로 묶는다."""

    rules = _between(_prompt("v3"), "## 항목별 규칙", "## 3단계. 변경 결정")
    parts = re.split(r"^### ", rules, flags=re.M)[1:]
    sections = {}
    for part in parts:
        heading = part.split("\n", 1)[0]
        match = re.match(r"`(\w+)` — .+", heading)
        if match:
            sections[match.group(1)] = part
    return sections


def test_v3_reads_top_to_bottom_in_working_order():
    """절이 작업 순서대로 놓여 있고, 그 밖의 최상위 절이 끼어 있지 않다."""

    assert tuple(re.findall(r"^## .*$", _prompt("v3"), re.M)) == _V3_HEADINGS


def test_v3_states_the_flow_and_which_section_wins():
    flow = _between(_prompt("v3"), "## 전체 작업 흐름", "## 1단계. 기록 읽기")

    steps = re.findall(r"^\d\. \*\*(.+?)\*\*", flow, re.M)
    assert steps == ["기록 읽기", "항목별 추론", "변경 결정", "문장 작성", "최종 검증"]
    assert "> " + " → ".join(steps) in flow
    # 단계마다 출력의 어느 자리를 채우는지 적는다.
    for number, field in (("3", "`reason`"), ("4", "`text`")):
        line = re.search(rf"^{number}\. .+$", flow, re.M).group(0)
        assert field in line, f"{number}단계가 채우는 출력 {field} 이 적혀 있지 않습니다."
    assert "앞 단계가 정한 것 위에 뒤 단계가 쌓입니다" in flow
    # 기본 규칙과 항목의 절이 다르게 적으면 그 절이 이긴다(성향의 충돌, memoryStyle 의 근거).
    assert "그 절이 기본값과 다르게 적으면 그 항목에서는 그 절을 따릅니다" in flow


def test_v3_has_one_rule_section_per_item_in_field_order():
    """고정 필드 열 개와 `customAttributes` 가 스키마의 순서대로 하나씩 있다."""

    assert tuple(_item_sections()) == _ITEM_SECTIONS


@pytest.mark.parametrize("item", _ITEM_SECTIONS)
def test_v3_item_section_holds_everything_needed_to_fill_that_item(item: str):
    """담는 것·근거·추론·갱신·근거가 약할 때·예시·피할 문장이 그 항목의 절 하나에 있다."""

    section = _item_sections()[item]

    positions = []
    for label in _ITEM_SECTION_LABELS:
        assert section.count(f"- {label}: ") + section.count(f"- {label}:\n") == 1, (
            f"`{item}` 절에 {label} 이 하나여야 합니다."
        )
        positions.append(section.index(f"- {label}:"))
    assert positions == sorted(positions), f"`{item}` 절의 항목 순서가 다른 절과 다릅니다."


@pytest.mark.parametrize("item", _ITEM_SECTIONS)
def test_v3_item_section_gives_inference_rules_as_evidence_to_conclusion(item: str):
    """추론 규칙은 `근거 → 알 수 있는 것` 이다. 정의만 있고 읽는 법이 없으면 모델이
    무엇에서 그 항목을 읽어 낼지 알 수 없다."""

    section = _item_sections()[item]
    inference = section[section.index("- **추론**:") : section.index("- **갱신**:")]

    if item == "customAttributes":
        assert "고정 필드와 같습니다" in inference
        return
    rules = re.findall(r"^  - .+ → .+$", inference, re.M)
    assert len(rules) >= 3, f"`{item}` 의 추론 규칙이 세 개보다 적습니다."


@pytest.mark.parametrize("item", _ITEM_SECTIONS)
def test_v3_item_example_and_bad_sentence_say_what_and_why(item: str):
    section = _item_sections()[item]

    example = re.search(r"^- \*\*예시\*\*: (.+)$", section, re.M).group(1)
    assert " → " in example, f"`{item}` 의 예시는 `기록 → 문장` 이어야 합니다."
    avoided = re.search(r"^- \*\*피할 문장\*\*: (.+)$", section, re.M).group(1)
    assert " — " in avoided, f"`{item}` 의 피할 문장에 이유가 없습니다."


def test_v3_explains_the_boundaries_between_confusable_items():
    """정의를 나열하는 것만으로는 어느 항목인지 갈리지 않는 짝들."""

    rules = _between(_prompt("v3"), "## 항목별 규칙", "## 3단계. 변경 결정")
    boundaries = rules[rules.index("### 헷갈리는 경계") :]

    for pair in (
        "**`basicProfile` 과 `lifeContext`**",
        "**`basicProfile` 과 `routines`**",
        "**`lifeContext` 와 `currentFocus`**",
        "**`routines` 와 `preferences`**",
        "**`personality` 와 `values`**",
        "**고정 필드와 `customAttributes`**",
    ):
        assert pair in boundaries, f"{pair} 의 경계가 없습니다."
    assert "가장 맞는 항목 한 곳에 적습니다" in boundaries


def test_v3_memory_style_is_read_from_the_memo_alone():
    """하루를 기억하는 방식은 사용자가 쓴 글에서만 읽힌다.

    `title`·`subtitle` 의 형식은 타임라인 AI 의 것이라, 거기서 읽으면 AI 가 쓰는 방식이
    사용자의 방식으로 적힌다.
    """

    section = _item_sections()["memoryStyle"]

    assert "**`memo` 만이 근거입니다.**" in section
    assert "`memo` 가 없으면 이 항목을 바꾸지 않습니다" in section


def test_v3_keeps_names_as_the_record_spells_them():
    """Timeline v3 는 프로필의 장소 이름·사람 이름·프로젝트 이름을 입력과 맞대어 읽는다."""

    sections = _item_sections()

    assert "기록에 나온 장소 이름 그대로" in sections["basicProfile"]
    assert "기록에 나온 그대로" in sections["relationships"]
    assert "기록에 나온 그대로" in sections["currentFocus"]


def test_v3_comparison_table_maps_every_case_to_an_action():
    """3단계의 표가 견준 결과마다 무엇을 하는지와 어느 동작인지를 말한다."""

    step = _between(_prompt("v3"), "## 3단계. 변경 결정", "## 4단계. 문장 작성")
    table = _between(step, "### 견준 결과 다섯 가지", "- **변경 목록에 넣는 항목은")
    # 첫 줄은 표의 머리다. 구분선(`|---|`)은 이 정규식에 걸리지 않는다.
    rows = re.findall(r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", table, re.M)[1:]

    assert [row[0].strip() for row in rows] == list(_COMPARED)
    actions = {action.value for action in UserMemoryChangeAction}
    for compared, _, todo, action in rows:
        named = set(re.findall(r"`(.+?)`", action))
        if compared.strip() == "이미 있음":
            assert action.strip() == "없음"
            assert "변경 목록에 넣지 않습니다" in todo
        else:
            assert named and named <= actions, f"`{compared}` 의 동작이 스키마에 없습니다."


def test_v3_tells_how_to_write_the_reason():
    """이유는 `견준 결과: 이번 기록의 사실` 이다. 코드는 읽지 않으므로 프롬프트가 전부다."""

    section = _between(_prompt("v3"), "### `reason` 쓰는 법", "기본 규칙은 다음과 같습니다.")

    assert "`견준 결과: 이번 기록의 사실`" in section
    examples = re.findall(r"^- `(.+?): .+`$", section, re.M)
    assert examples, "이유의 예가 없습니다."
    assert set(examples) <= (set(_COMPARED) - {"이미 있음"}) | {"크기"}
    for marker in (
        "`reason` 의 사실은 그 `item` 의 담는 것에 맞아야 합니다",
        "같은 사실을 이유로 두 항목을 바꾸지 않습니다",
        "이유를 지어내지 않습니다",
        "그 항목에 문장 수를 주었을 때만",
        "`reason` 을 `text` 에 옮겨 적지 않습니다",
    ):
        assert marker in section, f"「reason 쓰는 법」에 '{marker}' 가 없습니다."

    output = _between(_prompt("v3"), "## 출력 형식", "```json")
    assert output.index("- `reason`: ") < output.index("- `text`: ")
    assert "`text` 보다 먼저 적습니다" in output


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("**이번 기록에서 나온 것만** 셉니다", "기존 프로필을 옮겨 세면 바꿀 것이 없는 항목을 다시 냅니다."),
        ("같은 사실을 항목만 바꿔 두 번 적지 않습니다", "알게 된 것 하나가 여러 항목에 되풀이됩니다."),
        ("알게 된 것이 가리키지 않은 항목은 건드리지 않습니다", "변경마다 근거가 하나씩 있어야 합니다."),
        ("이유를 적을 수 없는 변경은 목록에 넣지 않습니다", "이유 없는 변경이 같은 문장을 다시 냅니다."),
        ("`한 번` 과 `한 번` 이 만나면 반복입니다", "이미 적혀 있다고 넘기면 반복으로 올라가지 않습니다."),
        ("**하루의 예외는 충돌이 아닙니다.** 본가에 다녀온 하루", "본가에 다녀온 하루가 사는 곳을 바꿉니다."),
        ("옛 정보는 적혀 있는 모든 항목에서 고칩니다", "이직한 뒤에도 옛 직장이 다른 항목에 남습니다."),
        ("그날 한 일을 하나씩 옮겨 적지 않습니다", "프로필이 타임라인의 요약이 됩니다."),
        ("속성 하나에는 그 키의 주제만 적습니다", "있던 속성에 상관없는 내용을 몰아 적습니다."),
        ("기존 프로필에 **없는 항목**", "내용이 있는 항목에 `추가` 를 쓰면 새 문장만 담깁니다."),
        ("**기존 내용은 지우지 않습니다.**", "이번 기록에 없다는 이유로 속성을 지웁니다."),
        ("`이번 기록에 없음` 은 지울 이유가 아닙니다", "이번 기록에 없다는 이유로 속성을 지웁니다."),
        ("**`[크기]` 가 속성 수를 줄이라고 했을 때만, 속성에만 씁니다.**", "`삭제` 를 평소에도 씁니다."),
        ("**적히지 않은 항목은 크기 때문에 줄이지 않습니다**", "몫을 받지 않은 항목까지 줄입니다."),
        ("달라진 내용으로 고쳐 씁니다", "취소된 계획이나 그만둔 일을 지웁니다."),
        ("기존 내용을 지우기만 하는 변경은 `삭제` 든 `수정` 이든 적용되지 않습니다", "코드가 하는 일과 프롬프트가 달라집니다."),
    ],
)
def test_v3_states_the_rules_the_live_runs_needed(marker: str, why: str):
    """실제 모델이 어긴 것들. 규칙이 빠지면 같은 잘못이 돌아온다."""

    assert marker in _prompt("v3"), f"user_memory v3 프롬프트에 '{marker}' 가 없습니다. {why}"


def test_v3_uses_custom_attributes_only_for_what_no_fixed_field_holds():
    """속성은 열 필드 어느 것에도 맞지 않는 정보의 자리다.

    이 규칙이 한 곳에만 약하게 적혀 있고 다른 절이 "다녀온 곳은 속성" 이라고 말했을 때,
    실제 모델은 부모님이 사는 곳을 `relationships` 와 속성 양쪽에 적었다. 프롬프트의
    어느 자리에서 읽어도 같은 말이어야 한다.
    """

    text = _prompt("v3")
    section = _item_sections()["customAttributes"]

    assert "어느 것의 정의에도 맞지 않는" in section
    assert "- **먼저 열 필드에 대 봅니다**: " in section
    assert "필드에 적은 것을 속성에 한 번 더 적지 않습니다" in section
    # 정의의 예시가 고정 필드의 몫을 가리키면 그 규칙과 어긋난다.
    definition = re.search(r"^- \*\*담는 것\*\*: (.+)$", section, re.M).group(1)
    for overlapping in ("자주 가는 곳", "즐겨 먹는 것", "취미"):
        assert overlapping not in definition, f"`{overlapping}` 은 고정 필드가 담습니다."
    # 피할 문장은 실제로 나온 잘못이다.
    assert "고정 필드(`relationships`)에 들어갈 정보를 속성으로 적음" in section

    assert "다녀온 곳과 해 본 경험은 `customAttributes` 입니다" not in text
    assert "열 필드 어디에도 맞지 않는 경험만 `customAttributes` 에 적습니다" in text
    assert "어디에도 맞지 않을 때만 속성을 만듭니다" in text
    check = _between(text, "## 5단계. 최종 검증", "## 출력 형식")
    assert "필드에 적은 것이 속성에 또 적혀 있지 않습니다" in check


def test_v3_final_check_covers_the_change_list_contract():
    check = _between(_prompt("v3"), "## 5단계. 최종 검증", "## 출력 형식")

    for marker in (
        "바꾸는 항목만",
        "한 항목은 변경 목록에 한 번만",
        "그 항목의 전체 문장",
        "남겨야 할 기존 문장",
        "`삭제` 의 `text` 는 `null`",
        "같은 정보가 두 항목에",
        "`[REDACTED_…]`",
    ):
        assert marker in check, f"최종 검증에 '{marker}' 가 없습니다."
