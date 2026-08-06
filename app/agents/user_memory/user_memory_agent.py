"""User Memory 갱신 Agent (#64).

기존 프로필과 확정된 하루 타임라인을 받아 **전체 갱신본 하나**를 만든다. append 가 아니라
rewrite 다 — 출력이 기존 값을 통째로 대체한다.

## 이 Agent 가 타임라인 파이프라인의 Agent 가 아닌 이유

:class:`~app.agents.base.Agent` 를 상속하지 않는다. 그 인터페이스는
``generate(request: TimelineDraftRequest)`` 로 타임라인 한 건의 처리를 표현하는데,
여기 입력은 여러 날의 확정 기록이고 출력도 타임라인이 아니다. 형태를 억지로 맞추면
"이것도 타임라인 단계 중 하나" 로 읽힌다.

## AI 가 쓴 문장에서 성향을 뽑지 않는다

입력의 ``title``·``subtitle``·``question`` 은 **이 시스템의 타임라인 AI 가 쓴
문장**이다. 거기서 성격·가치관·취향을 읽어 프로필에 넣으면, 모델이 자기 출력을 읽고
사용자를 만들어 내는 되먹임이 된다. 그렇게 쌓이는 것은 사용자가 아니라 프롬프트의
문체이고, 그 프로필이 다시 다음 타임라인 문장을 만드는 데 쓰이므로 한 번 시작되면
스스로를 강화한다.

사용자의 실제 발화는 ``memo`` 뿐이고 **비어 있을 수 있다.** 메모 없는 날은 성향 계열
필드가 그대로인 것이 정상이며, 그것은 실패가 아니다. 이 규칙을 프롬프트에 명시하지
않으면 모델은 반드시 AI 문장에서 성향을 만들어 낸다.

## 무엇을 LLM 이 정하고 무엇을 코드가 정하는가

- **LLM**: 무엇을 남기고 합치고 버릴지 (의미 판단)
- **코드**: 얼마나 클 수 있는지, 무엇이 남으면 안 되는지, 몇 번까지 다시 물을지
  (셀 수 있는 것)

``schemaVersion`` 과 ``updatedAt`` 도 코드가 정한다. 계약 버전과 갱신 시각은 관측
가능한 사실이지 모델의 판단이 아니다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from app.agents.parsing import SupportsComplete, default_llm, user_memory_to_text
from app.agents.prompt_loader import load_prompt
from app.core.execution_context import ExecutionStage, execution_scope
from app.core.logging import get_logger, log_fields
from app.schemas.user_memory import UserMemory
from app.services.user_memory_limits import DailyTimelineDigest

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")

#: 갱신은 창작이 아니라 정리다. 표현을 흔들 이유가 없어 낮게 둔다.
_TEMPERATURE = 0.2


def build_update_prompt(
    existing: UserMemory | None,
    digest: DailyTimelineDigest,
    *,
    violations: Sequence[str] = (),
) -> str:
    """갱신 요청 user prompt 를 만든다.

    ``existing`` 은 다른 Agent 와 **같은 projection**(:func:`user_memory_to_text`)으로
    싣는다. 같은 메모리가 자리마다 다른 문자열이 되면 어느 쪽을 근거로 판단했는지
    재현할 수 없다.

    ``violations`` 는 직전 출력이 어긴 규칙이다(:mod:`app.services.user_memory_repair`
    가 채운다). 값은 인용하지 않고 어느 필드가 어떤 규칙을 어겼는지만 담긴다.
    """

    sections = [
        f"[existing user memory]\n{user_memory_to_text(existing)}",
        f"[dailyTimelines]\n{json.dumps(digest.daily_timelines, ensure_ascii=False, indent=2)}",
    ]

    if not digest.has_memo:
        # 모델이 빈 자리를 메우려 드는 것을 막는다. "근거가 없다" 를 명시적으로
        # 알려 주지 않으면 AI 가 쓴 title/subtitle 에서 성향을 만들어 낸다.
        sections.append(
            "[근거 없음]\n"
            "이번 기록에는 사용자가 직접 쓴 memo 가 하나도 없습니다. "
            "personality·values·preferences·emotionalPatterns·memoryStyle 은 "
            "기존 값을 그대로 두세요. 생활 구조 쪽(routines·lifeContext·currentFocus)만 "
            "event 구조를 근거로 갱신합니다."
        )

    if violations:
        listed = "\n".join(f"- {item}" for item in violations)
        sections.append(
            "[직전 출력이 규칙을 어겼습니다]\n"
            f"{listed}\n"
            "위 지적을 반영해 User Memory 전체를 다시 만드세요."
        )

    sections.append(
        "위 기록을 반영해 **User Memory 전체**를 다시 만드세요. "
        "기존 프로필에 덧붙이는 것이 아니라 병합·수정·압축·삭제를 거친 최신 상태 "
        "하나를 출력합니다."
    )
    return "\n\n".join(sections)


class UserMemoryAgent:
    """확정된 하루 타임라인으로 User Memory 전체 갱신본을 만든다."""

    name = "user-memory"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def generate(
        self,
        existing: UserMemory | None,
        digest: DailyTimelineDigest,
        *,
        violations: Sequence[str] = (),
    ) -> UserMemory:
        """전체 갱신본을 만든다.

        스키마 검증(필드 200자·``customAttributes`` 5개·모르는 최상위 필드)은
        ``complete_structured`` 안의 교정 재시도가 맡는다. 크기 총량과 민감정보는
        그 위에서 :mod:`app.services.user_memory_repair` 가 본다.

        실패는 삼키지 않고 그대로 올린다 — 코드 부여와 기록은 흡수하는 쪽의 몫이다.
        """

        prompt = build_update_prompt(existing, digest, violations=violations)
        with execution_scope(ExecutionStage.USER_MEMORY_AGENT, agent=self.name):
            logger.debug(
                "User Memory 갱신 요청",
                extra=log_fields(
                    hasExistingMemory=existing is not None,
                    repairHints=len(violations),
                    **digest.stats,
                ),
            )
            return self.llm.complete_structured(
                prompt,
                UserMemory,
                system=_SYSTEM_PROMPT,
                temperature=_TEMPERATURE,
            )
