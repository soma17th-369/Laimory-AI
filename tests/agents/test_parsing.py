"""공용 파싱 유틸 검증."""

import pytest
from pydantic import ValidationError

from app.agents.parsing import (
    build_infer_prompt,
    items_to_text,
    parse_agent_result,
    user_memory_to_text,
)
from app.schemas import UserMemory
from tests.fixtures.fake_llm import candidate, fragment, result_json
from tests.fixtures.requests import DAY_START, HOUR, stay


def test_parse_strips_code_fence_and_surrounding_text():
    text = "다음과 같습니다:\n```json\n" + result_json() + "\n```"
    result = parse_agent_result(text)
    assert result.candidates == []
    assert result.fragments == []


def test_parse_raises_when_no_json():
    with pytest.raises(ValueError):
        parse_agent_result("JSON 이 없습니다.")


def test_parse_rejects_invalid_schema():
    bad = result_json(candidates=[candidate("NOT_A_TYPE", [("LOCATION", "s-1")])])
    with pytest.raises(ValidationError):
        parse_agent_result(bad)


def test_parse_requires_fragment_summary():
    bad = result_json(fragments=[{"sourceType": "PHOTO", "sourceId": "photo-1"}])
    with pytest.raises(ValidationError):
        parse_agent_result(bad)


def test_parse_accepts_fragment_source_id_and_summary():
    result = parse_agent_result(
        result_json(fragments=[fragment("PHOTO", "photo-1", "저녁 사진")])
    )
    assert result.fragments[0].source_id == "photo-1"
    assert result.fragments[0].summary == "저녁 사진"


def test_items_to_text_includes_source_id():
    text = items_to_text([stay("stay-1", 37.5, 127.0, DAY_START, DAY_START + HOUR)])
    assert "stay-1" in text


def test_user_memory_to_text_handles_none_and_values():
    assert user_memory_to_text(None) == "정보 없음"
    assert "student" in user_memory_to_text(UserMemory(job="student"))


def test_build_infer_prompt_contains_sections():
    prompt = build_infer_prompt("나이 30", "데이터")
    assert "사용자 정보" in prompt
    assert "나이 30" in prompt
    assert "분석할 데이터" in prompt
