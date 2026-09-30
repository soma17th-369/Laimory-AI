"""User Memory 갱신본 확정 (#64, #121).

계약은 세 가지다.

- 규칙을 어긴 갱신본은 **저장 문서가 되지 않는다**(1304). 코드가 문장을 자르지 않는다.
- 기본은 **다시 요청하지 않는다**(#121). 갱신 한 건은 LLM 호출 한 번이다. 재요청 경로는
  남아 있고 ``max_attempts`` 를 올리면 직전 출력을 돌려주고 고치게 한다.
- ``schemaVersion``·``updatedAt`` 은 **서버가 정한다**(LLM 값을 쓰지 않는다).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.error_codes import ErrorCode
from app.schemas.user_memory import NARRATIVE_MAX_LENGTH, SCHEMA_VERSION, UserMemory
from app.services.user_memory_limits import build_daily_timeline_digest, serialized_chars
from app.services.user_memory_repair import (
    MAX_REPAIR_ATTEMPTS,
    UserMemoryLimitError,
    build_user_memory,
    finalize,
)

_KST = timezone(timedelta(hours=9), "Asia/Seoul")
_NOW = datetime(2026, 8, 6, 9, 0, tzinfo=_KST)


class _StubAgent:
    """호출 순서대로 준비한 메모리를 돌려주고, 받은 지적과 직전 출력을 기록한다."""

    def __init__(self, memories: list[UserMemory]) -> None:
        self._memories = list(memories)
        self.violations_seen: list[list[str]] = []
        self.previous_seen: list[UserMemory | None] = []

    def generate(self, existing, digest, *, violations=(), previous=None) -> UserMemory:
        self.violations_seen.append(list(violations))
        self.previous_seen.append(previous)
        index = min(len(self.violations_seen) - 1, len(self._memories) - 1)
        return self._memories[index]


def _digest():
    return build_daily_timeline_digest([])


def _oversized() -> UserMemory:
    """필드는 저마다 상한 안인데 합치면 전체 상한(2,000자)을 넘는 문서."""

    return UserMemory(
        **{
            field: "가" * NARRATIVE_MAX_LENGTH
            for field in (
                "basic_profile",
                "life_context",
                "relationships",
                "personality",
                "values",
            )
        }
    )


def test_clean_output_passes_without_a_retry():
    agent = _StubAgent([UserMemory(basic_profile="30대 개발자입니다.")])

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert outcome.repair_attempts == 0
    assert agent.violations_seen == [[]]
    assert outcome.memory.basic_profile == "30대 개발자입니다."


# --- 기본: 다시 요청하지 않는다 (#121) -----------------------------------


def test_default_is_no_retry():
    assert MAX_REPAIR_ATTEMPTS == 0


def test_oversized_output_fails_after_a_single_call():
    """상한을 넘은 갱신본을 같은 작업 안에서 다시 요청하지 않는다.

    두 번째 응답이 규칙 안이어도 묻지 않으므로 쓰이지 않는다. 저장 문서는 만들어지지
    않고 기존 프로필이 그대로 남는다.
    """

    agent = _StubAgent([_oversized(), UserMemory(basic_profile="짧게 줄였습니다.")])

    with pytest.raises(UserMemoryLimitError) as caught:
        build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert caught.value.code is ErrorCode.USER_MEMORY_LIMIT_EXCEEDED
    assert len(agent.violations_seen) == 1
    assert agent.previous_seen == [None]


def test_sensitive_output_fails_after_a_single_call():
    agent = _StubAgent([UserMemory(relationships="엄마 010-1234-5678")])

    with pytest.raises(UserMemoryLimitError):
        build_user_memory(agent, None, _digest(), updated_at=_NOW)

    assert len(agent.violations_seen) == 1


def test_failure_reports_the_size_without_the_content():
    """재요청이 없으면 중간 로그도 없다. 실패 하나만 보고 상한 초과인지 알 수 있어야 한다."""

    memory = _oversized()
    agent = _StubAgent([memory])

    with pytest.raises(UserMemoryLimitError) as caught:
        build_user_memory(agent, None, _digest(), updated_at=_NOW)

    message = str(caught.value)
    assert f"serializedChars={serialized_chars(memory)}" in message
    assert "attempts=1" in message
    assert "가가" not in message


# --- 재요청 경로 (max_attempts 를 올렸을 때) ------------------------------


def test_violation_is_sent_back_and_the_second_answer_is_kept():
    agent = _StubAgent([_oversized(), UserMemory(basic_profile="짧게 줄였습니다.")])

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW, max_attempts=1)

    assert outcome.repair_attempts == 1
    # 1차는 지적 없이, 2차는 지적을 붙여 물었다.
    assert agent.violations_seen[0] == []
    assert agent.violations_seen[1] and "상한" in agent.violations_seen[1][0]
    assert outcome.memory.basic_profile == "짧게 줄였습니다."


def test_retry_hands_back_the_document_that_broke_the_rule():
    """재요청은 직전 출력을 **고치게** 한다(#121).

    지적만 붙여 처음부터 다시 만들게 하면 매번 같은 입력에서 출발해 같은 크기의 문서가
    다시 나온다. 상한에 닿은 프로필로 실측했을 때 재요청 세 번이 상한을 끝내 넘지 못했다.
    """

    first, second = _oversized(), _oversized().model_copy(update={"routines": "가" * 400})
    agent = _StubAgent([first, second, UserMemory(basic_profile="줄였습니다.")])

    outcome = build_user_memory(agent, None, _digest(), updated_at=_NOW, max_attempts=2)

    assert outcome.repair_attempts == 2
    # 1차에는 고칠 문서가 없고, 그 뒤로는 바로 앞 시도의 출력을 받는다.
    assert agent.previous_seen == [None, first, second]


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
