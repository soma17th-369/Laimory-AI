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
    UserMemoryPatch,
)


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


# --- 부분 갱신 (#121) ---------------------------------------------------
#
# 모델에게는 바꿀 항목만 받고 코드가 기존 문서에 끼워 넣는다. 여기서 지키는 것은
# "패치에 없는 항목은 글자 하나 바뀌지 않는다" 하나다.


def _patch(**overrides) -> UserMemoryPatch:
    return UserMemoryPatch.model_validate(overrides)


def _profile() -> UserMemory:
    return _memory(
        updatedAt="2026-09-27T21:00:00+09:00",
        basicProfile="망원동에 사는 30대 개발자입니다.",
        routines="평일에는 회사에서 일합니다.",
        customAttributes={"반려동물": "고양이 한 마리", "여행": "8월 말 강릉"},
    )


def test_empty_patch_changes_nothing():
    profile = _profile()

    assert _patch().apply_to(profile) == profile


def test_patch_replaces_only_the_fields_it_carries():
    profile = _profile()

    updated = _patch(routines="평일에는 회사에서 일하고 주말에 클라이밍을 합니다.").apply_to(profile)

    assert updated.routines == "평일에는 회사에서 일하고 주말에 클라이밍을 합니다."
    assert updated.basic_profile == profile.basic_profile
    assert updated.custom_attributes == profile.custom_attributes
    assert updated.updated_at == profile.updated_at


def test_patch_does_not_mutate_the_original():
    profile = _profile()
    before = profile.model_dump()

    _patch(
        routines="바뀐 문장입니다.",
        customAttributes=[{"key": "여행", "value": None}],
    ).apply_to(profile)

    assert profile.model_dump() == before


def test_null_leaves_a_field_and_an_empty_string_clears_it():
    """``None`` 은 "바꾸지 않는다" 이고 빈 문자열은 "비운다" 다. 둘을 섞으면 안 된다."""

    profile = _profile()

    untouched = _patch(basicProfile=None).apply_to(profile)
    cleared = _patch(basicProfile="").apply_to(profile)

    assert untouched.basic_profile == profile.basic_profile
    assert cleared.basic_profile == ""


def test_patch_adds_replaces_and_removes_custom_attributes():
    updated = _patch(
        customAttributes=[
            {"key": "운동", "value": "합정 클라이밍장"},
            {"key": "반려동물", "value": "고양이 두 마리"},
            {"key": "여행", "value": None},
        ]
    ).apply_to(_profile())

    assert updated.custom_attributes == {"반려동물": "고양이 두 마리", "운동": "합정 클라이밍장"}


@pytest.mark.parametrize("value", [None, ""])
def test_removing_a_custom_attribute_takes_null_or_an_empty_value(value):
    updated = _patch(customAttributes=[{"key": "여행", "value": value}]).apply_to(_profile())

    assert "여행" not in updated.custom_attributes


def test_removing_an_unknown_custom_attribute_is_a_no_op():
    profile = _profile()

    updated = _patch(customAttributes=[{"key": "없는 키", "value": None}]).apply_to(profile)

    assert updated.custom_attributes == profile.custom_attributes


def test_later_change_wins_when_a_key_appears_twice():
    updated = _patch(
        customAttributes=[
            {"key": "운동", "value": "수영"},
            {"key": "운동", "value": "클라이밍"},
        ]
    ).apply_to(_profile())

    assert updated.custom_attributes["운동"] == "클라이밍"


def test_patch_applies_to_a_missing_profile_as_to_an_empty_one():
    """기존 문서가 없는 것과 비어 있는 것을 가를 이유가 없다."""

    patch = _patch(basicProfile="판교 회사에 다니는 직장인으로 보입니다.")

    assert patch.apply_to(None) == patch.apply_to(UserMemory())
    assert patch.apply_to(None).prompt_payload() == {
        "basicProfile": "판교 회사에 다니는 직장인으로 보입니다."
    }


def test_patch_value_over_the_item_limit_is_rejected():
    """항목 값 하나의 길이 제한은 저장 문서와 같다. 패치를 통과한 값은 적용한 뒤에도
    문서 계약을 어기지 않는다."""

    with pytest.raises(ValidationError):
        _patch(routines="가" * (NARRATIVE_MAX_LENGTH + 1))
    with pytest.raises(ValidationError):
        _patch(
            customAttributes=[
                {"key": "메모", "value": "가" * (CUSTOM_ATTRIBUTE_MAX_LENGTH + 1)}
            ]
        )


def test_patch_rejects_unknown_fields_and_metadata():
    """패치로 계약 버전이나 갱신 시각을 바꿀 수 없다. 그 값은 서버가 정한다."""

    with pytest.raises(ValidationError):
        _patch(favoriteColor="파랑")
    with pytest.raises(ValidationError):
        _patch(schemaVersion="9.9")
    with pytest.raises(ValidationError):
        _patch(customAttributes=[{"key": "", "value": "값"}])


def test_patch_can_change_exactly_the_items_the_document_has():
    """문서에만 필드를 더하면 그 필드는 영영 갱신되지 않는다."""

    patchable = {
        field.alias or name for name, field in UserMemoryPatch.model_fields.items()
    }

    assert patchable == set(NARRATIVE_FIELDS) | {"customAttributes"}


def test_patch_schema_can_be_enforced_by_the_provider():
    """문서 스키마는 ``customAttributes`` 가 자유형 dict 라 strict 로 표현되지 않는다.
    패치는 (키, 값) 목록이라 provider 가 모양을 강제할 수 있다."""

    assert to_strict_schema(UserMemory) is None
    assert to_strict_schema(UserMemoryPatch) is not None


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
