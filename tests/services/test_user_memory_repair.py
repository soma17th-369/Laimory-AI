"""User Memory 갱신본 확정 (#64, #121).

계약은 세 가지다.

- 규칙을 어긴 갱신본은 **저장 문서가 되지 않는다**(1304). 코드가 문장을 자르지 않는다.
- **다시 요청하지 않는다**(#121). 갱신 한 건은 LLM 호출 한 번이다.
- ``schemaVersion``·``updatedAt`` 은 **서버가 정한다**(LLM 값을 쓰지 않는다).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.error_codes import ErrorCode
from app.schemas.user_memory import NARRATIVE_MAX_LENGTH, SCHEMA_VERSION, UserMemory
from app.services.user_memory_limits import build_daily_timeline_digest, serialized_chars
from app.services.user_memory_repair import (
    UserMemoryLimitError,
    build_user_memory,
    count_changes,
    finalize,
)

_KST = timezone(timedelta(hours=9), "Asia/Seoul")
_NOW = datetime(2026, 8, 6, 9, 0, tzinfo=_KST)


class _StubAgent:
    """호출 순서대로 준비한 메모리를 돌려주고 호출 횟수를 센다."""

    def __init__(self, memories: list[UserMemory]) -> None:
        self._memories = list(memories)
        self.calls = 0

    def generate(self, existing, digest) -> UserMemory:
        self.calls += 1
        return self._memories[min(self.calls - 1, len(self._memories) - 1)]


def _digest():
    return build_daily_timeline_digest([])


def _oversized() -> UserMemory:
    """필드는 저마다 상한 안인데 합치면 전체 상한을 넘는 문서.

    열 필드가 모두 제한까지 차면 직렬화에 드는 키와 따옴표만큼 넘는다.
    """

    return UserMemory(
        **{
            field: "가" * NARRATIVE_MAX_LENGTH
            for field in (
                "basic_profile",
                "life_context",
                "relationships",
                "personality",
                "values",
                "preferences",
                "routines",
                "current_focus",
                "emotional_patterns",
                "memory_style",
            )
        }
    )


def test_clean_output_becomes_the_document():
    agent = _StubAgent([UserMemory(basic_profile="30대 개발자입니다.")])

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert agent.calls == 1
    assert outcome.memory.basic_profile == "30대 개발자입니다."


# --- 다시 요청하지 않는다 (#121) ------------------------------------------


def test_oversized_output_fails_after_a_single_call():
    """상한을 넘은 갱신본을 같은 작업 안에서 다시 요청하지 않는다.

    두 번째 응답이 규칙 안이어도 묻지 않으므로 쓰이지 않는다. 저장 문서는 만들어지지
    않고 기존 프로필이 그대로 남는다.
    """

    agent = _StubAgent([_oversized(), UserMemory(basic_profile="짧게 줄였습니다.")])

    with pytest.raises(UserMemoryLimitError) as caught:
        build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert caught.value.code is ErrorCode.USER_MEMORY_LIMIT_EXCEEDED
    assert agent.calls == 1


def test_sensitive_output_fails_after_a_single_call():
    """규칙을 어긴 프로필을 저장하느니 기존 값을 그대로 두는 편이 낫다."""

    agent = _StubAgent([UserMemory(relationships="엄마 010-1234-5678")])

    with pytest.raises(UserMemoryLimitError):
        build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert agent.calls == 1


def test_failure_reports_the_size_without_the_content():
    """실패 하나만 보고 상한 초과인지 알 수 있어야 한다. 값은 싣지 않는다."""

    memory = _oversized()
    agent = _StubAgent([memory])

    with pytest.raises(UserMemoryLimitError) as caught:
        build_user_memory(agent, None, _digest(), updated_at=_NOW)

    message = str(caught.value)
    assert f"serializedChars={serialized_chars(memory)}" in message
    assert "violations=1" in message
    assert "가가" not in message


def test_agent_is_called_with_the_profile_and_the_digest_only():
    """재요청이 없으므로 지적도 직전 출력도 넘기지 않는다."""

    seen = {}

    class _Agent:
        def generate(self, *args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            return UserMemory(basic_profile="30대 개발자입니다.")

    existing = UserMemory(routines="평일에는 회사에서 일합니다.")
    digest = _digest()

    build_user_memory(_Agent(), existing, digest, updated_at=_NOW)

    assert seen == {"args": (existing, digest), "kwargs": {}}


# --- 무엇이 달라졌는가 (#121) ---------------------------------------------


def _profile() -> UserMemory:
    return UserMemory(
        basic_profile="망원동에 사는 개발자입니다.",
        routines="평일에는 회사에서 일합니다.",
        custom_attributes={"반려동물": "고양이", "여행": "강릉"},
    )


def test_unchanged_profile_counts_no_changes():
    profile = _profile()

    assert count_changes(profile, profile.model_copy()) == (0, 0)


def test_changes_are_counted_by_comparing_the_two_documents():
    """모델이 낸 것을 세지 않고 두 문서를 비교한다. 값이 같으면 바뀐 것이 아니다."""

    before = _profile()
    after = before.model_copy(
        update={
            "routines": "평일에는 회사에서 일하고 주말에 클라이밍을 합니다.",
            "personality": "계획을 세워 움직입니다.",
            "custom_attributes": {"반려동물": "고양이 두 마리", "운동": "클라이밍"},
        }
    )

    # 필드: routines 교체 + personality 추가. 속성: 교체 1 + 추가 1 + 삭제 1.
    assert count_changes(before, after) == (2, 3)


def test_first_profile_counts_everything_it_fills():
    assert count_changes(None, _profile()) == (2, 2)


def test_metadata_is_not_a_change():
    """``updatedAt`` 은 갱신마다 바뀐다. 그것을 세면 언제나 "바뀌었다" 가 된다."""

    before = _profile()
    after = finalize(before, updated_at=_NOW)

    assert count_changes(before, after) == (0, 0)


def test_outcome_reports_how_much_changed():
    existing = _profile()
    updated = existing.model_copy(update={"routines": "주말에 클라이밍을 합니다."})
    agent = _StubAgent([updated])

    outcome = build_user_memory(agent, existing, _digest(), updated_at=_NOW)

    assert outcome.changed_field_count == 1
    assert outcome.changed_attribute_count == 0


def test_metadata_comes_from_the_server_not_the_model():
    """모델이 정하게 두면 언젠가 "우리가 모르는 버전" 이 저장돼 다음 날 읽기가 깨진다."""

    agent = _StubAgent(
        [
            UserMemory.model_construct(
                schema_version="9.9",
                updated_at="1999-01-01T00:00:00+09:00",
                basic_profile="30대 개발자입니다.",
                life_context="",
                relationships="",
                personality="",
                values="",
                preferences="",
                routines="",
                current_focus="",
                emotional_patterns="",
                memory_style="",
                custom_attributes={},
            )
        ]
    )

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert outcome.memory.schema_version == SCHEMA_VERSION
    assert outcome.memory.updated_at == _NOW.isoformat()


def test_finalize_keeps_the_content_untouched():
    memory = UserMemory(basic_profile="30대 개발자입니다.", custom_attributes={"a": "b"})

    stamped = finalize(memory, updated_at=_NOW)

    assert stamped.basic_profile == memory.basic_profile
    assert stamped.custom_attributes == {"a": "b"}
    assert stamped.updated_at == "2026-08-06T09:00:00+09:00"
