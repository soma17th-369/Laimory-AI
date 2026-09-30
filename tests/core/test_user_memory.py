"""User Memory v1.0 계약과 프롬프트 projection (#65).

여기서 지키는 것은 두 가지다.

1. **계약을 어긴 값은 조용히 통과하지 않는다.** 모르는 최상위 필드, 지원하지 않는
   버전, 길이 초과는 전부 거절한다. 흡수는 이 뒤(입력 조회 경계)의 판단이고,
   스키마 자신은 애매하게 받아 주지 않는다. ``customAttributes`` 의 개수는 세지
   않는다(#121) — 끝은 전체 크기 상한이 막는다.
2. **같은 메모리는 언제나 같은 문자열이 된다.** 6개 Agent 가 같은 문자열을 봐야
   무엇을 근거로 판단했는지 재현할 수 있다.
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.parsing import user_memory_to_text
from app.schemas import UserMemory
from app.core.structured import to_strict_schema
from app.schemas.user_memory import (
    CUSTOM_ATTRIBUTE_MAX_LENGTH,
    METADATA_FIELDS,
    NARRATIVE_FIELDS,
    NARRATIVE_MAX_LENGTH,
    SCHEMA_VERSION,
    UserMemoryChange,
    UserMemoryChangeAction,
    UserMemoryPatch,
)
from tests.fixtures.user_memory import change


def _memory(**overrides) -> UserMemory:
    return UserMemory.model_validate({"schemaVersion": SCHEMA_VERSION, **overrides})


# --- 계약 ---------------------------------------------------------------


def test_empty_memory_is_valid_and_versioned():
    memory = UserMemory()

    assert memory.schema_version == SCHEMA_VERSION
    assert memory.updated_at is None
    assert memory.custom_attributes == {}
    assert memory.prompt_payload() == {}


def test_unknown_top_level_field_is_rejected():
    with pytest.raises(ValidationError) as exc:
        _memory(favoriteColor="파랑")

    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_unsupported_schema_version_is_rejected():
    with pytest.raises(ValidationError) as exc:
        UserMemory.model_validate({"schemaVersion": "2.0"})

    assert exc.value.errors()[0]["type"] == "literal_error"


def test_narrative_field_over_limit_is_rejected():
    with pytest.raises(ValidationError) as exc:
        _memory(basicProfile="가" * (NARRATIVE_MAX_LENGTH + 1))

    assert exc.value.errors()[0]["type"] == "string_too_long"


def test_narrative_field_at_limit_is_accepted():
    memory = _memory(basicProfile="가" * NARRATIVE_MAX_LENGTH)

    assert len(memory.basic_profile) == NARRATIVE_MAX_LENGTH


def test_field_and_custom_attribute_limits_are_500_chars():
    """값이 바뀌면 프롬프트·문서가 말하는 숫자도 함께 바뀌어야 한다(#121)."""

    assert NARRATIVE_MAX_LENGTH == 500
    assert CUSTOM_ATTRIBUTE_MAX_LENGTH == 500


def test_custom_attributes_have_no_count_limit():
    """한 번 나온 정보도 남기는 정책이라 개수를 세지 않는다(#121).

    개수를 세면 새 정보를 담으려고 옛 정보를 버리게 된다. 예전 상한은 5개였다.
    """

    attributes = {f"키{index}": "값" for index in range(30)}

    memory = _memory(customAttributes=attributes)

    assert len(memory.custom_attributes) == 30


def test_custom_attribute_value_over_limit_is_rejected():
    with pytest.raises(ValidationError) as exc:
        _memory(customAttributes={"메모": "가" * (CUSTOM_ATTRIBUTE_MAX_LENGTH + 1)})

    assert exc.value.errors()[0]["type"] == "string_too_long"


def test_custom_attribute_value_at_limit_is_accepted():
    memory = _memory(customAttributes={"메모": "가" * CUSTOM_ATTRIBUTE_MAX_LENGTH})

    assert len(memory.custom_attributes["메모"]) == CUSTOM_ATTRIBUTE_MAX_LENGTH


def test_memory_written_under_the_old_limits_still_reads():
    """상한은 넓어지기만 했다. 예전 계약(200자·5개·150자)으로 쓴 문서는 그대로 읽힌다."""

    memory = _memory(
        basicProfile="가" * 200,
        customAttributes={f"키{index}": "나" * 150 for index in range(5)},
    )

    assert len(memory.basic_profile) == 200
    assert len(memory.custom_attributes) == 5


# --- projection ---------------------------------------------------------


def test_prompt_payload_omits_empty_fields_and_metadata():
    memory = _memory(
        updatedAt="2026-08-05T11:00:00+09:00",
        basicProfile="30대 개발자",
        relationships="",
    )

    payload = memory.prompt_payload()

    assert payload == {"basicProfile": "30대 개발자"}
    for name in METADATA_FIELDS:
        assert name not in payload


def test_prompt_payload_follows_declaration_order_regardless_of_input_order():
    values = {name: f"{name} 값" for name in NARRATIVE_FIELDS}

    forward = UserMemory.model_validate(values).prompt_payload()
    reversed_input = UserMemory.model_validate(
        dict(reversed(list(values.items())))
    ).prompt_payload()

    assert list(forward) == list(NARRATIVE_FIELDS)
    assert list(reversed_input) == list(NARRATIVE_FIELDS)


def test_custom_attributes_are_kept_when_present():
    memory = _memory(customAttributes={"반려동물": "고양이 두 마리"})

    assert memory.prompt_payload() == {
        "customAttributes": {"반려동물": "고양이 두 마리"}
    }


def test_missing_memory_and_empty_memory_read_the_same():
    """비어 있는 메모리는 없는 것과 같다. Agent 가 구분할 이유가 없다."""

    assert user_memory_to_text(None) == "정보 없음"
    assert user_memory_to_text(UserMemory()) == "정보 없음"


def test_projection_text_is_stable_json():
    memory = _memory(basicProfile="30대 개발자", currentFocus="ASM 프로젝트")

    text = user_memory_to_text(memory)

    assert json.loads(text) == {
        "basicProfile": "30대 개발자",
        "currentFocus": "ASM 프로젝트",
    }
    # 한글을 이스케이프하면 같은 뜻에 토큰만 늘어난다.
    assert "\\u" not in text
    assert text == user_memory_to_text(memory)


# --- 변경 목록 (#121) ---------------------------------------------------
#
# 모델에게는 "어느 항목을 추가·수정·삭제할지" 만 받고 코드가 기존 문서에 끼워 넣는다.
# 여기서 지키는 것은 "목록에 없는 항목은 글자 하나 바뀌지 않는다" 하나다.


def _patch(*changes: dict) -> UserMemoryPatch:
    return UserMemoryPatch.model_validate({"changes": list(changes)})


def _profile() -> UserMemory:
    return _memory(
        updatedAt="2026-09-27T21:00:00+09:00",
        basicProfile="망원동에 사는 30대 개발자입니다.",
        routines="평일에는 회사에서 일합니다.",
        customAttributes={"반려동물": "고양이 한 마리", "여행": "8월 말 강릉"},
    )


def test_empty_change_list_changes_nothing():
    """바꿀 것이 없으면 빈 목록이다. 실패가 아니다."""

    profile = _profile()

    assert _patch().apply_to(profile) == profile
    assert UserMemoryPatch.model_validate({}).apply_to(profile) == profile


def test_change_replaces_only_the_item_it_names():
    profile = _profile()

    updated = _patch(
        change("routines", "수정", "평일에는 회사에서 일하고 주말에 클라이밍을 합니다.")
    ).apply_to(profile)

    assert updated.routines == "평일에는 회사에서 일하고 주말에 클라이밍을 합니다."
    assert updated.basic_profile == profile.basic_profile
    assert updated.custom_attributes == profile.custom_attributes
    assert updated.updated_at == profile.updated_at


def test_change_list_does_not_mutate_the_original():
    profile = _profile()
    before = profile.model_dump()

    _patch(
        change("routines", "수정", "바뀐 문장입니다."),
        change("customAttributes.여행", "삭제"),
    ).apply_to(profile)

    assert profile.model_dump() == before


def test_update_replaces_the_item_with_the_text():
    """`수정` 은 바꿔 끼운다. 합치는 것은 의미 판단이라 모델의 몫이다."""

    updated = _patch(change("routines", "수정", "주말에 클라이밍을 합니다.")).apply_to(
        _profile()
    )

    assert updated.routines == "주말에 클라이밍을 합니다."


@pytest.mark.parametrize("action", ["추가", "수정"])
def test_empty_item_is_filled_by_either_action(action: str):
    """비어 있던 항목인지 있던 항목인지를 모델이 잘못 짚어도 변경을 버리지 않는다."""

    filled = _patch(change("lifeContext", action, "마감을 앞둔 시기입니다.")).apply_to(
        _profile()
    )

    assert filled.life_context == "마감을 앞둔 시기입니다."


def test_remove_clears_a_fixed_field():
    updated = _patch(change("basicProfile", "삭제")).apply_to(_profile())

    assert updated.basic_profile == ""
    assert "basicProfile" not in updated.prompt_payload()


def test_changes_add_replace_and_remove_custom_attributes():
    updated = _patch(
        change("customAttributes.운동", "추가", "합정 클라이밍장"),
        change("customAttributes.반려동물", "수정", "고양이 두 마리"),
        change("customAttributes.여행", "삭제"),
    ).apply_to(_profile())

    assert updated.custom_attributes == {
        "반려동물": "고양이 두 마리",
        "운동": "합정 클라이밍장",
    }


def test_custom_attribute_key_is_everything_after_the_prefix():
    """키에는 공백이 있을 수 있다(`자주 가는 카페`)."""

    parsed = UserMemoryChange.model_validate(
        change("customAttributes.자주 가는 카페", "추가", "망원동 카페")
    )

    assert parsed.attribute_key == "자주 가는 카페"
    assert UserMemoryChange.model_validate(change("routines", "삭제")).attribute_key is None


def test_removing_an_unknown_custom_attribute_is_a_no_op():
    profile = _profile()

    updated = _patch(change("customAttributes.없는 키", "삭제")).apply_to(profile)

    assert updated.custom_attributes == profile.custom_attributes


def test_later_change_wins_when_an_item_appears_twice():
    updated = _patch(
        change("customAttributes.운동", "추가", "수영"),
        change("customAttributes.운동", "수정", "클라이밍"),
        change("routines", "수정", "첫 문장입니다."),
        change("routines", "수정", "둘째 문장입니다."),
    ).apply_to(_profile())

    assert updated.custom_attributes["운동"] == "클라이밍"
    assert updated.routines == "둘째 문장입니다."


def test_change_list_applies_to_a_missing_profile_as_to_an_empty_one():
    """기존 문서가 없는 것과 비어 있는 것을 가를 이유가 없다."""

    patch = _patch(change("basicProfile", "추가", "판교 회사에 다니는 직장인으로 보입니다."))

    assert patch.apply_to(None) == patch.apply_to(UserMemory())
    assert patch.apply_to(None).prompt_payload() == {
        "basicProfile": "판교 회사에 다니는 직장인으로 보입니다."
    }


def test_text_over_the_item_limit_is_rejected():
    """항목 값 하나의 길이 제한은 저장 문서와 같다. 변경을 통과한 값은 적용한 뒤에도
    문서 계약을 어기지 않는다."""

    with pytest.raises(ValidationError):
        _patch(change("routines", "수정", "가" * (NARRATIVE_MAX_LENGTH + 1)))
    with pytest.raises(ValidationError):
        _patch(
            change("customAttributes.메모", "추가", "가" * (CUSTOM_ATTRIBUTE_MAX_LENGTH + 1))
        )

    at_limit = _patch(change("routines", "수정", "가" * NARRATIVE_MAX_LENGTH))
    assert len(at_limit.apply_to(None).routines) == NARRATIVE_MAX_LENGTH


def test_length_error_names_the_limit_without_quoting_the_text():
    """오류 문장은 교정 재시도 프롬프트와 로그에 실린다. 값을 옮겨 적지 않는다."""

    with pytest.raises(ValidationError) as exc:
        _patch(change("routines", "수정", "가나다라" * 200))

    message = exc.value.errors(include_input=False)[0]["msg"]
    assert f"{NARRATIVE_MAX_LENGTH}자" in message
    assert "가나다라" not in message


@pytest.mark.parametrize("action", ["추가", "수정"])
@pytest.mark.parametrize("text", [None, "", "   "])
def test_add_and_update_need_a_text(action: str, text):
    """비우려면 `삭제` 를 쓴다. 빈 문장으로 채우는 변경은 뜻이 없다."""

    with pytest.raises(ValidationError):
        _patch(change("routines", action, text))


def test_remove_ignores_whatever_text_it_carries():
    updated = _patch(change("customAttributes.여행", "삭제", "지울 속성입니다.")).apply_to(
        _profile()
    )

    assert "여행" not in updated.custom_attributes


@pytest.mark.parametrize(
    "item",
    [
        "favoriteColor",
        "schemaVersion",
        "updatedAt",
        "customAttributes",
        "customAttributes.",
        "customAttributes.  ",
    ],
)
def test_change_cannot_name_an_item_the_document_does_not_have(item: str):
    """변경 목록으로 계약 버전이나 갱신 시각을 바꿀 수 없다. 그 값은 서버가 정한다."""

    with pytest.raises(ValidationError):
        _patch(change(item, "수정", "값"))


def test_unknown_action_and_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        _patch(change("routines", "교체", "값"))
    with pytest.raises(ValidationError):
        _patch({**change("routines", "수정", "값"), "note": "메모"})
    with pytest.raises(ValidationError):
        UserMemoryPatch.model_validate({"changes": [], "routines": "값"})


def test_every_fixed_field_can_be_changed():
    """문서에만 필드를 더하면 그 필드는 영영 갱신되지 않는다."""

    patch = _patch(*(change(name, "추가", f"{name} 값") for name in NARRATIVE_FIELDS))

    assert patch.apply_to(None).prompt_payload() == {
        name: f"{name} 값" for name in NARRATIVE_FIELDS
    }


def test_actions_are_the_three_the_prompt_names():
    assert [action.value for action in UserMemoryChangeAction] == ["추가", "수정", "삭제"]


def test_change_list_schema_can_be_enforced_by_the_provider():
    """문서 스키마는 ``customAttributes`` 가 자유형 dict 라 strict 로 표현되지 않는다.
    변경 목록은 (항목, 동작, 문장) 의 목록이라 provider 가 모양을 강제할 수 있다."""

    assert to_strict_schema(UserMemory) is None
    assert to_strict_schema(UserMemoryPatch) is not None


def test_change_list_schema_carries_no_design_notes_to_the_provider():
    """클래스 docstring 은 JSON schema 의 description 으로 provider 에 나간다.

    설계 이유를 docstring 에 길게 적으면 그것이 매 요청에 실린다.
    """

    schema = json.dumps(to_strict_schema(UserMemoryPatch), ensure_ascii=False)

    assert len(schema) < 1_500
    assert "#121" not in schema
    assert "실측" not in schema


def test_reason_comes_before_text_in_the_schema():
    """선언 순서가 곧 모델이 쓰는 순서다. 이유가 문장보다 먼저 와야 그것을 보고 쓴다."""

    schema = to_strict_schema(UserMemoryPatch)

    assert list(schema["properties"]) == ["changes"]
    assert list(schema["$defs"]["UserMemoryChange"]["properties"]) == [
        "item",
        "action",
        "reason",
        "text",
    ]


@pytest.mark.parametrize("reason", ["반복: 클라이밍을 이번 주말에도 함", "", "아무 말"])
def test_reason_is_never_read(reason: str):
    """`reason` 은 모델의 판단 과정이다. 코드는 읽지 않고, 비어 있어도 거절하지 않는다."""

    body = {**change("routines", "수정", "주말에 클라이밍을 합니다."), "reason": reason}

    updated = _patch(body).apply_to(_profile())

    assert updated.routines == "주말에 클라이밍을 합니다."
    assert reason == "" or reason not in json.dumps(
        updated.model_dump(by_alias=True), ensure_ascii=False
    )


def test_change_without_a_reason_is_still_a_change():
    """이유가 빠졌다고 변경을 버리지 않는다. 생각을 적는 자리가 비었을 뿐이다."""

    parsed = UserMemoryChange.model_validate(change("routines", "삭제"))

    assert parsed.reason == ""


# --- 관측 ---------------------------------------------------------------


def test_trace_summary_carries_no_body():
    memory = _memory(
        basicProfile="경기도에 사는 개발자",
        relationships="엄마와 매주 통화",
        customAttributes={"반려동물": "고양이"},
    )

    summary = memory.trace_summary()

    assert summary["schemaVersion"] == SCHEMA_VERSION
    assert summary["filledFieldCount"] == 2
    assert summary["customAttributeCount"] == 1
    assert summary["serializedChars"] > 0

    serialized = json.dumps(summary, ensure_ascii=False)
    for body in ("경기도", "개발자", "엄마", "고양이", "반려동물"):
        assert body not in serialized
