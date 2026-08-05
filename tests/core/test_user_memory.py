"""User Memory v1.0 계약과 프롬프트 projection (#65).

여기서 지키는 것은 두 가지다.

1. **계약을 어긴 값은 조용히 통과하지 않는다.** 모르는 최상위 필드, 지원하지 않는
   버전, 길이·개수 초과는 전부 거절한다. 흡수는 이 뒤(입력 조회 경계)의 판단이고,
   스키마 자신은 애매하게 받아 주지 않는다.
2. **같은 메모리는 언제나 같은 문자열이 된다.** 6개 Agent 가 같은 문자열을 봐야
   무엇을 근거로 판단했는지 재현할 수 있다.
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.parsing import user_memory_to_text
from app.schemas import UserMemory
from app.schemas.user_memory import (
    CUSTOM_ATTRIBUTE_MAX_COUNT,
    CUSTOM_ATTRIBUTE_MAX_LENGTH,
    METADATA_FIELDS,
    NARRATIVE_FIELDS,
    NARRATIVE_MAX_LENGTH,
    SCHEMA_VERSION,
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


def test_too_many_custom_attributes_are_rejected():
    attributes = {f"키{index}": "값" for index in range(CUSTOM_ATTRIBUTE_MAX_COUNT + 1)}

    with pytest.raises(ValidationError):
        _memory(customAttributes=attributes)


def test_custom_attribute_value_over_limit_is_rejected():
    with pytest.raises(ValidationError) as exc:
        _memory(customAttributes={"메모": "가" * (CUSTOM_ATTRIBUTE_MAX_LENGTH + 1)})

    assert exc.value.errors()[0]["type"] == "string_too_long"


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
