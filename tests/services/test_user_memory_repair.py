"""User Memory 갱신본 확정 (#64).

계약은 세 가지다.

- 규칙을 어기면 **다시 묻는다**(코드가 문장을 자르지 않는다).
- 재요청까지 실패하면 **저장 문서를 만들지 않는다**(1304).
- ``schemaVersion``·``updatedAt`` 은 **서버가 정한다**(LLM 값을 쓰지 않는다).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.error_codes import ErrorCode
from app.schemas.user_memory import SCHEMA_VERSION, UserMemory
from app.services.user_memory_limits import build_daily_timeline_digest
from app.services.user_memory_repair import (
    UserMemoryLimitError,
    build_user_memory,
    finalize,
)

_KST = timezone(timedelta(hours=9), "Asia/Seoul")
_NOW = datetime(2026, 8, 6, 9, 0, tzinfo=_KST)


class _StubAgent:
    """호출 순서대로 준비한 메모리를 돌려주고, 받은 지적을 기록한다."""

    def __init__(self, memories: list[UserMemory]) -> None:
        self._memories = list(memories)
        self.violations_seen: list[list[str]] = []

    def generate(self, existing, digest, *, violations=()) -> UserMemory:
        self.violations_seen.append(list(violations))
        index = min(len(self.violations_seen) - 1, len(self._memories) - 1)
        return self._memories[index]


def _digest():
    return build_daily_timeline_digest([])


def _oversized() -> UserMemory:
    return UserMemory(
        **{
            field: "가" * 200
            for field in (
                "basic_profile",
                "life_context",
                "relationships",
                "personality",
                "values",
                "preferences",
                "routines",
            )
        }
    )


def test_clean_output_passes_without_a_retry():
    agent = _StubAgent([UserMemory(basic_profile="30대 개발자입니다.")])

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert outcome.repair_attempts == 0
    assert agent.violations_seen == [[]]
    assert outcome.memory.basic_profile == "30대 개발자입니다."


def test_violation_is_sent_back_and_the_second_answer_is_kept():
    agent = _StubAgent([_oversized(), UserMemory(basic_profile="짧게 줄였습니다.")])

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert outcome.repair_attempts == 1
    # 1차는 지적 없이, 2차는 지적을 붙여 물었다.
    assert agent.violations_seen[0] == []
    assert agent.violations_seen[1] and "상한" in agent.violations_seen[1][0]
    assert outcome.memory.basic_profile == "짧게 줄였습니다."


def test_exhausted_retries_produce_no_document():
    """규칙을 어긴 프로필을 저장하느니 기존 값을 그대로 두는 편이 낫다."""

    agent = _StubAgent([_oversized()])

    with pytest.raises(UserMemoryLimitError) as caught:
        build_user_memory(agent, None, _digest(), updated_at=_NOW, max_attempts=2)

    assert caught.value.code is ErrorCode.USER_MEMORY_LIMIT_EXCEEDED
    # 1차 + 재요청 2회.
    assert len(agent.violations_seen) == 3


def test_sensitive_output_never_becomes_a_document():
    agent = _StubAgent([UserMemory(relationships="엄마 010-1234-5678")])

    with pytest.raises(UserMemoryLimitError):
        build_user_memory(agent, None, _digest(), updated_at=_NOW, max_attempts=1)


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
