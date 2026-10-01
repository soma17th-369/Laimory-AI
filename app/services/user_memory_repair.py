"""User Memory 갱신본 확정 (#64).

Agent 가 만든 문서가 크기·민감정보 규칙을 지켰는지 본다. 어겼으면 **저장 문서를 만들지
않는다.** 규칙을 어긴 프로필을 저장하느니 기존 값을 그대로 두는 편이 낫다 — 갱신은 App
Server 의 다음 배치가 다시 시도한다.

코드가 문장을 자르지 않는다 — 무엇을 합치고 무엇을 지울지는 전부 의미 판단이고, 잘린
문장은 뜻이 달라진다. 그걸 근거로 쓴 해석은 되돌릴 방법이 없다.
:mod:`app.services.duration_guard` 가 "자르거나 나누지 않는다" 고 한 것과 같은 이유다.

## 다시 요청하지 않는다 (#121)

갱신 한 건은 LLM 호출 한 번이다. 한 번 만들어 규칙 안이면 저장하고, 아니면 1304 로
끝낸다. 예전에는 규칙을 어긴 갱신본을 지적과 함께 두 번까지 다시 요청했다.

v3 세트는 여기까지 오기 전에 **규칙을 어긴 변경만 뺀다**
(:func:`~app.services.user_memory_limits.apply_changes`). 그래서 v3 세트가 여기서
걸리는 것은 받은 문서가 이미 규칙을 어긴 경우뿐이다. 문서 전체를 받는 v1·v2 세트는
항목을 가려 뺄 수 없어 지금처럼 여기서 걸린다.

모듈 이름의 repair 는 재요청이 있던 때의 이름이다. 지금 하는 일은 확정뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.schemas.user_memory import NARRATIVE_FIELDS, SCHEMA_VERSION, UserMemory
from app.services.user_memory_limits import find_violations, serialized_chars


class UserMemoryLimitError(AppError):
    """갱신본이 크기·민감정보 규칙을 통과하지 못했다."""

    default_code = ErrorCode.USER_MEMORY_LIMIT_EXCEEDED


@dataclass(frozen=True)
class UserMemoryOutcome:
    """확정된 갱신본과, 그것이 기존 문서에서 얼마나 달라졌는지."""

    memory: UserMemory
    #: 기존 문서와 값이 달라진 고정 필드의 수. 0 이면 이번 기록이 고정 필드를 바꾸지
    #: 않은 것이다.
    changed_field_count: int = 0
    #: 더하거나 바꾸거나 지운 ``customAttributes`` 의 수.
    changed_attribute_count: int = 0


def count_changes(before: UserMemory | None, after: UserMemory) -> tuple[int, int]:
    """기존 문서와 갱신본 사이에 달라진 (고정 필드 수, 속성 수).

    모델이 낸 변경 목록을 세지 않고 **두 문서를 비교한다.** 목록에 담겼어도 값이 같으면
    바뀐 것이 아니고, 문서 전체를 다시 쓰는 세트(v1·v2)에서도 같은 숫자가 나온다.
    돌려주는 것은 개수뿐이다 — 어느 항목이 무엇으로 바뀌었는지는 본문이라 남기지 않는다.
    """

    old = (before or UserMemory()).prompt_payload()
    new = after.prompt_payload()
    fields = sum(
        1 for name in NARRATIVE_FIELDS if old.get(name, "") != new.get(name, "")
    )
    old_attributes = old.get("customAttributes", {})
    new_attributes = new.get("customAttributes", {})
    attributes = sum(
        1
        for key in old_attributes.keys() | new_attributes.keys()
        if old_attributes.get(key) != new_attributes.get(key)
    )
    return fields, attributes


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
) -> UserMemoryOutcome:
    """갱신본을 한 번 만들고 규칙을 통과하면 확정한다.

    여기서 보는 것은 Pydantic 이 표현할 수 없는 두 가지 — **전체 크기**와 **민감정보**다.

    Args:
        agent: :class:`~app.agents.user_memory.UserMemoryAgent` 또는 같은 형태의 더블.
        existing: 기존 프로필. 최초 생성이면 ``None``.
        digest: 프롬프트에 실을 하루 기록(:class:`~app.services.user_memory_limits.DailyTimelineDigest`).
        updated_at: 갱신 시각. 호출부가 정한다(테스트가 시간을 고정할 수 있게).

    Raises:
        UserMemoryLimitError: 규칙을 통과하지 못했다(1304).
    """

    memory = agent.generate(existing, digest)
    violations = find_violations(memory)
    if violations:
        # 값은 싣지 않는다. 크기와 개수만으로 "상한을 넘었는가, 민감정보였는가" 를 가른다.
        raise UserMemoryLimitError(
            "User Memory 갱신본이 규칙을 통과하지 못했습니다: "
            f"violations={len(violations)}, serializedChars={serialized_chars(memory)}"
        )

    changed_fields, changed_attributes = count_changes(existing, memory)
    return UserMemoryOutcome(
        memory=finalize(memory, updated_at=updated_at),
        changed_field_count=changed_fields,
        changed_attribute_count=changed_attributes,
    )
