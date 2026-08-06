"""User Memory 갱신본 확정 (#64).

Agent 가 만든 문서가 크기·민감정보 규칙을 지켰는지 보고, 어겼으면 **위반 내용을
붙여 다시 요청한다.** 코드가 문장을 자르지 않는다 — 압축 5단계(중복 제거 → 오래된
관심사 제거 → 영향 적은 정보 제거 → 문장 병합 → customAttributes 제거)는 전부 의미
판단이고, 잘린 문장은 뜻이 달라진다. 그걸 근거로 쓴 해석은 되돌릴 방법이 없다.
:mod:`app.services.duration_guard` 가 "자르거나 나누지 않는다" 고 한 것과 같은 이유다.

재시도까지 실패하면 **저장 문서를 만들지 않는다.** 규칙을 어긴 프로필을 저장하느니
기존 값을 그대로 두는 편이 낫다 — 갱신은 매일 다시 시도된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.core.logging import get_logger, log_fields
from app.schemas.user_memory import SCHEMA_VERSION, UserMemory
from app.services.user_memory_limits import find_violations, serialized_chars

logger = get_logger(__name__)

#: 규칙 위반 시 다시 물어볼 횟수. 1차 + 재요청 2회 = 최대 3회 호출이다.
MAX_REPAIR_ATTEMPTS = 2


class UserMemoryLimitError(AppError):
    """갱신본이 재요청 뒤에도 크기·민감정보 규칙을 통과하지 못했다."""

    default_code = ErrorCode.USER_MEMORY_LIMIT_EXCEEDED


@dataclass(frozen=True)
class UserMemoryOutcome:
    """확정된 갱신본과 거기까지 걸린 재요청 횟수."""

    memory: UserMemory
    #: 규칙 위반으로 **다시 물어본** 횟수. 1차에 통과하면 0 이다.
    repair_attempts: int


def finalize(memory: UserMemory, *, updated_at: datetime) -> UserMemory:
    """서버가 정하는 메타데이터를 박아 저장 가능한 문서로 만든다.

    ``schemaVersion`` 과 ``updatedAt`` 은 LLM 값을 쓰지 않는다. 계약 버전과 갱신
    시각은 관측 가능한 사실이지 모델의 판단이 아니고, 모델이 정하게 두면 언젠가
    "우리가 모르는 버전" 이 저장돼 다음 날 읽기가 실패한다.
    """

    return memory.model_copy(
        update={
            "schema_version": SCHEMA_VERSION,
            "updated_at": updated_at.isoformat(),
        }
    )


def build_user_memory(
    agent,
    existing: UserMemory | None,
    digest,
    *,
    updated_at: datetime,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> UserMemoryOutcome:
    """규칙을 통과하는 갱신본이 나올 때까지 요청하고 확정한다.

    필드별 길이와 ``customAttributes`` 개수는 그 아래
    (``complete_structured`` 의 교정 재시도)에서 이미 걸러진다. 여기서 보는 것은
    Pydantic 이 표현할 수 없는 두 가지 — **전체 크기**와 **민감정보**다.

    Args:
        agent: :class:`~app.agents.user_memory.UserMemoryAgent` 또는 같은 형태의 더블.
        existing: 기존 프로필. 최초 생성이면 ``None``.
        digest: 프롬프트에 실을 하루 기록(:class:`~app.services.user_memory_limits.DailyTimelineDigest`).
        updated_at: 갱신 시각. 호출부가 정한다(테스트가 시간을 고정할 수 있게).
        max_attempts: 위반 시 다시 물어볼 횟수.

    Raises:
        UserMemoryLimitError: 재요청까지 소진하고도 규칙을 통과하지 못했다(1304).
    """

    violations: list[str] = []
    for attempt in range(max_attempts + 1):
        memory = agent.generate(existing, digest, violations=violations)
        violations = find_violations(memory)
        if not violations:
            return UserMemoryOutcome(
                memory=finalize(memory, updated_at=updated_at),
                repair_attempts=attempt,
            )
        # 위반 문장에는 값이 들어 있지 않다(어느 필드가 어떤 규칙을 어겼는지만).
        # 그래도 개수만 남긴다 — 지적 문구까지 매 시도 로그에 쌓을 이유가 없다.
        logger.info(
            "User Memory 갱신본이 규칙을 어겨 다시 요청합니다.",
            extra=log_fields(
                attempt=attempt + 1,
                maxAttempts=max_attempts + 1,
                violationCount=len(violations),
                serializedChars=serialized_chars(memory),
            ),
        )

    raise UserMemoryLimitError(
        "User Memory 갱신본이 재요청 뒤에도 규칙을 통과하지 못했습니다: "
        f"attempts={max_attempts + 1}, violations={len(violations)}"
    )
