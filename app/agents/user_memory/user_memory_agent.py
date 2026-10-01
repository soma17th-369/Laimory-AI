"""User Memory 갱신 Agent (#64, #121).

기존 프로필과 확정된 하루 타임라인을 받아 **갱신된 프로필 문서 하나**를 돌려준다.
App Server 로 나가는 것은 언제나 문서 전체이고, 그것이 기존 값을 통째로 대체한다.

## 모델이 무엇을 출력하는지는 세트마다 다르다

- **v3** 는 **변경 목록**을 출력한다(:class:`~app.schemas.user_memory.UserMemoryPatch`).
  변경 한 건은 어느 항목을 추가·수정·삭제할지와 그 항목의 새 문장이다. 코드가 그것을
  기존 문서에 끼워 넣는다. 목록에 없는 항목은 글자 하나 바뀌지 않는다.
- **v1·v2** 는 문서 전체를 다시 출력한다. 그 프롬프트가 그렇게 적혀 있고 v2 는 운영
  세트다.

전체를 다시 쓰게 하면 바뀐 것이 한 줄이어도 나머지 필드를 전부 다시 쓴다. 건드릴 이유가
없던 내용이 조금씩 달라지거나 빠지고(실측에서 "사는 곳" 이 줄이는 과정에서 사라졌다),
문서가 상한에 닿으면 다시 쓰는 양이 곧 상한이라 넘기기 쉽다.

## 이 Agent 가 타임라인 파이프라인의 Agent 가 아닌 이유

:class:`~app.agents.base.Agent` 를 상속하지 않는다. 그 인터페이스는
``generate(request: TimelineDraftRequest)`` 로 타임라인 한 건의 처리를 표현하는데,
여기 입력은 여러 날의 확정 기록이고 출력도 타임라인이 아니다. 형태를 억지로 맞추면
"이것도 타임라인 단계 중 하나" 로 읽힌다.

## 무엇을 근거로 읽는지는 프롬프트 세트가 정한다

입력의 ``title``·``subtitle``·``question`` 은 **이 시스템의 타임라인 AI 가 쓴
문장**이고, 사용자가 직접 남긴 것은 ``memo`` 와 하루 감정(``emotion``)뿐이다. 이 출처를
어떻게 다루는지가 세트마다 다르다.

- **v1·v2** 는 AI 가 쓴 문장에서 성향을 뽑지 않는다(#64). 모델이 자기 출력을 읽고
  사용자를 만들어 내는 되먹임을 막으려는 것이다. 성향 계열 필드의 근거는 ``memo`` 뿐이고,
  메모 없는 날은 그 필드가 그대로인 것이 정상이다.
- **v3** 는 AI 가 쓴 문장도 근거로 읽는다(#121). 사용자가 읽고 저장한 기록이므로
  받아들인 내용으로 본다. ``memo`` 를 쓰지 않는 사용자의 프로필이 자라지 않는 것이 더 큰
  문제라고 판단했다. 되먹임은 없어지지 않는다 — 프롬프트가 문장의 표현이 아니라 사실을
  읽게 하고, 사용자가 직접 남긴 것과 어긋나면 그쪽을 따르게 해서 줄인다.

그래서 이 모듈은 근거 정책을 갖지 않는다. 하나 남은 예외가 :data:`_MEMO_ONLY_TRAITS`
이고 v1·v2 의 동작을 그대로 지키려고 둔 것이다.

## 무엇을 LLM 이 정하고 무엇을 코드가 정하는가

- **LLM**: 어느 항목을 바꾸고 그 항목에 무엇을 남기고 합치고 버릴지 (의미 판단)
- **코드**: 바꾼 항목을 문서에 끼워 넣는 일, 얼마나 클 수 있는지, 무엇이 남으면 안
  되는지 (셀 수 있는 것)

``schemaVersion`` 과 ``updatedAt`` 도 코드가 정한다. 계약 버전과 갱신 시각은 관측
가능한 사실이지 모델의 판단이 아니다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from app.agents.parsing import SupportsComplete, default_llm, user_memory_to_text
from app.agents.prompt_loader import load_prompt, uses_legacy_contract
from app.core.execution_context import ExecutionStage, execution_scope
from app.core.logging import get_logger, log_fields
from app.core.llm_stages import LLMStage
from app.schemas.user_memory import UserMemory, UserMemoryPatch
from app.services.user_memory_limits import (
    USER_MEMORY_MAX_CHARS,
    USER_MEMORY_TARGET_CHARS,
    DailyTimelineDigest,
    apply_changes,
    serialized_chars,
    shrink_budget,
)

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")

#: 갱신은 창작이 아니라 정리다. 표현을 흔들 이유가 없어 낮게 둔다.
_TEMPERATURE = 0.2

#: 성향 계열 필드의 근거를 ``memo`` 로만 제한하는 세트인가(#121).
#:
#: v1·v2 가 그렇다. 그 세트에서는 메모 없는 날에 "근거 없음" 을 user prompt 로 한 번 더
#: 알린다. v3 는 AI 가 쓴 문장도 근거로 읽으므로 그 지시가 시스템 프롬프트와 정면으로
#: 어긋난다. 지시를 통째로 지우지 않고 가른 것은 ``PROMPT_VERSION`` 을 v2 로 되돌렸을 때
#: 그 세트의 근거 정책이 예전처럼 지켜져야 하기 때문이다.
#:
#: 어느 세트가 예전 계약인지는 프롬프트 세트 전체가 같은 기준을 쓴다
#: (:func:`~app.agents.prompt_loader.uses_legacy_contract`).
_MEMO_ONLY_TRAITS = uses_legacy_contract()

#: 모델이 문서 전체가 아니라 **변경 목록**(바꿀 항목만)을 출력하는 세트인가(#121).
#:
#: v3 부터 그렇다. v1·v2 프롬프트는 문서 전체를 출력하라고 적혀 있어 그대로 전체를 받는다.
#: 두 분기값은 같은 기준에서 나오지만 하는 일이 달라 이름을 따로 둔다.
_PATCH_OUTPUT = not uses_legacy_contract()


def _size_section(existing: UserMemory | None) -> str | None:
    """기존 프로필의 크기를 목표·상한과 함께 알려 준다(#121). 비어 있으면 ``None``.

    모델은 글자 수를 세지 못한다. 기존 프로필이 얼마나 큰지 모르면 새 정보를 그대로
    얹어 상한을 넘기고, 넘은 프로필은 저장되지 않는다. 실측에서 이 절 없이는 상한에
    닿은 프로필의 갱신이 대부분 실패했다.

    여기서 주는 것은 **숫자와 순서**뿐이다(먼저 줄이고 나서 더한다). 무엇을 줄일지는
    시스템 프롬프트의 정책이고 세트마다 다르다. 기존 프로필이 목표를 넘었으면 어느
    항목을 몇 문장까지 줄일지도 함께 준다 — 모델이 따르는 단위가 글자 수가 아니라 문장
    수이기 때문이다(:func:`~app.services.user_memory_limits.shrink_budget`). 줄일 몫은
    넘은 만큼만이고, 몫을 받지 않은 항목은 적지 않는다.
    """

    if existing is None or not existing.prompt_payload():
        return None

    size = serialized_chars(existing)
    lines = [
        "[크기]",
        f"기존 프로필은 {size}자입니다. 갱신본의 목표 크기는 "
        f"{USER_MEMORY_TARGET_CHARS}자이고, {USER_MEMORY_MAX_CHARS}자를 넘으면 "
        "저장되지 않습니다.",
    ]
    budget = shrink_budget(existing)
    if budget:
        lines.append(
            f"기존 프로필이 이미 목표를 {size - USER_MEMORY_TARGET_CHARS}자 넘었습니다. "
            "새 정보를 더하기 전에 기존 내용을 아래 항목마다 적힌 문장 수 이내로 먼저 "
            "줄이세요. 아래에 적히지 않은 항목은 줄이지 않습니다."
        )
        lines.extend(f"  - {line}" for line in budget)
    return "\n".join(lines)


def build_update_prompt(
    existing: UserMemory | None,
    digest: DailyTimelineDigest,
    *,
    violations: Sequence[str] = (),
    previous: UserMemory | None = None,
) -> str:
    """갱신 요청 user prompt 를 만든다.

    ``existing`` 은 다른 Agent 와 **같은 projection**(:func:`user_memory_to_text`)으로
    싣는다. 같은 메모리가 자리마다 다른 문자열이 되면 어느 쪽을 근거로 판단했는지
    재현할 수 없다.

    ``violations`` 는 직전 출력이 어긴 규칙이다(:mod:`app.services.user_memory_repair`
    가 채운다). 값은 인용하지 않고 어느 필드가 어떤 규칙을 어겼는지만 담긴다.

    ``previous`` 는 그 규칙을 어긴 직전 출력이다(#121). 주어지면 처음부터 다시 만들지
    않고 **그 문서를 고치게** 한다. 지적만 붙여 다시 만들게 하면 매번 같은 입력에서
    출발하므로 같은 크기의 문서가 다시 나온다 — 실측에서 재요청 세 번이 2,085 →
    2,062 → 2,011자로 상한(2,000자)을 끝내 넘지 못했다. 고칠 문서를 주면 시도마다
    앞의 결과에서 이어 줄인다.
    """

    sections = [
        f"[existing user memory]\n{user_memory_to_text(existing)}",
        f"[dailyTimelines]\n{json.dumps(digest.daily_timelines, ensure_ascii=False, indent=2)}",
    ]

    size_section = _size_section(existing)
    if size_section:
        sections.append(size_section)

    if _MEMO_ONLY_TRAITS and not digest.has_memo:
        # 모델이 빈 자리를 메우려 드는 것을 막는다. "근거가 없다" 를 명시적으로
        # 알려 주지 않으면 AI 가 쓴 title/subtitle 에서 성향을 만들어 낸다.
        sections.append(
            "[근거 없음]\n"
            "이번 기록에는 사용자가 직접 쓴 memo 가 하나도 없습니다. "
            "personality·values·preferences·emotionalPatterns·memoryStyle 은 "
            "기존 값을 그대로 두세요. 생활 구조 쪽(routines·lifeContext·currentFocus)만 "
            "event 구조를 근거로 갱신합니다."
        )

    if violations and previous is not None:
        listed = "\n".join(f"- {item}" for item in violations)
        sections.append(f"[직전 출력]\n{user_memory_to_text(previous)}")
        sections.append(f"[직전 출력이 규칙을 어겼습니다]\n{listed}")
        # 직전 출력에는 이번 기록이 이미 반영돼 있다. 다시 만들면 그 작업을 버리고
        # 같은 자리에서 출발한다.
        if _PATCH_OUTPUT:
            sections.append(
                "처음부터 다시 만들지 말고 **직전 출력에서 바꿀 항목만** `changes` 에 "
                "담아 출력하세요. 지적된 규칙에 맞게 고칠 항목의 `text` 에 그 항목의 "
                "전체 문장을 적고, 나머지 항목은 적지 않습니다."
            )
        else:
            sections.append(
                "처음부터 다시 만들지 말고 **직전 출력을 고쳐** User Memory 전체를 "
                "출력하세요. 지적된 규칙에 맞게 고치고, 바뀐 부분만이 아니라 문서 전체를 "
                "출력합니다."
            )
        return "\n\n".join(sections)

    if violations:
        listed = "\n".join(f"- {item}" for item in violations)
        sections.append(
            "[직전 출력이 규칙을 어겼습니다]\n"
            f"{listed}\n"
            "위 지적을 반영해 User Memory 전체를 다시 만드세요."
        )

    # 무엇을 남기고 버릴지는 여기서 말하지 않는다. 그것은 시스템 프롬프트의 정책이고
    # 세트마다 다르다. 여기서 고정하는 것은 **출력의 모양**뿐이다 — 변경 목록인지
    # 문서 전체인지.
    if _PATCH_OUTPUT:
        sections.append(
            "위 기록을 반영해 기존 프로필에서 **바꿀 항목만** `changes` 에 담아 "
            "출력하세요. 변경마다 왜 바꾸는지를 `reason` 에 먼저 적고, `text` 에는 그 "
            "항목의 전체 문장을 적습니다. 바꾸지 않는 항목은 적지 않습니다. 바꿀 것이 "
            "없으면 `changes` 를 빈 목록으로 둡니다."
        )
    else:
        sections.append(
            "위 기록을 반영해 **User Memory 전체**를 다시 만드세요. "
            "바뀐 부분만이 아니라 기존 정보와 새 정보를 합친 전체 갱신본 하나를 "
            "출력합니다."
        )
    return "\n\n".join(sections)


class UserMemoryAgent:
    """확정된 하루 타임라인으로 갱신된 User Memory 문서를 만든다."""

    name = "user-memory"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.USER_MEMORY)
        return self._llm

    def generate(
        self,
        existing: UserMemory | None,
        digest: DailyTimelineDigest,
        *,
        violations: Sequence[str] = (),
        previous: UserMemory | None = None,
    ) -> UserMemory:
        """갱신된 문서 전체를 돌려준다.

        v3 세트에서는 모델이 변경 목록을 내고(:class:`UserMemoryPatch`) 여기서 기존
        문서에 끼워 넣는다. v1·v2 세트에서는 모델이 낸 문서 전체를 그대로 돌려준다.
        어느 쪽이든 **반환값은 문서 전체**라 호출부는 세트를 몰라도 된다.

        출력의 모양(JSON 인지, 모르는 키가 없는지)은 ``complete_structured`` 안의 교정
        재시도가 맡는다.

        v3 세트에서는 **규칙을 어긴 변경만 뺀다**
        (:func:`~app.services.user_memory_limits.apply_changes`). 없는 항목을 가리키거나,
        항목 길이를 넘거나, 민감한 값이 있거나, 기존 내용을 지우기만 하거나, 적용하면
        문서가 전체 상한을 넘는 변경이다. 그 항목은 기존 내용 그대로 남고 나머지 변경은
        적용된다. 다시 요청하지 않는다.

        v1·v2 세트는 문서 전체를 받으므로 항목을 가려 뺄 수 없다. 항목 길이는 교정
        재시도가, 크기 총량과 민감정보는 :mod:`app.services.user_memory_repair` 가 본다.

        ``previous`` 는 규칙을 어긴 직전 출력이다. 주어지면 그 문서를 고쳐서 낸다 —
        변경 목록도 기존 문서가 아니라 그 문서에 적용한다.

        실패는 삼키지 않고 그대로 올린다 — 코드 부여와 기록은 흡수하는 쪽의 몫이다.
        """

        prompt = build_update_prompt(
            existing, digest, violations=violations, previous=previous
        )
        with execution_scope(ExecutionStage.USER_MEMORY_AGENT, agent=self.name):
            logger.debug(
                "User Memory 갱신 요청",
                extra=log_fields(
                    hasExistingMemory=existing is not None,
                    repairHints=len(violations),
                    patchOutput=_PATCH_OUTPUT,
                    **digest.stats,
                ),
            )
            if not _PATCH_OUTPUT:
                return self.llm.complete_structured(
                    prompt,
                    UserMemory,
                    system=_SYSTEM_PROMPT,
                    temperature=_TEMPERATURE,
                )

            patch = self.llm.complete_structured(
                prompt,
                UserMemoryPatch,
                system=_SYSTEM_PROMPT,
                temperature=_TEMPERATURE,
            )
            base = previous if violations and previous is not None else existing
            # 규칙을 어긴 변경은 그것만 뺀다. 그 항목은 기존 내용 그대로 남고 나머지
            # 변경은 적용된다 — 변경 하나 때문에 그날의 갱신을 통째로 버리지 않는다.
            memory, dropped = apply_changes(patch, base)
            if dropped:
                # 어느 항목이었는지는 본문에 가까워 남기지 않는다. 이유와 개수만 남긴다.
                logger.info(
                    "규칙을 어긴 User Memory 변경을 적용하지 않았습니다.",
                    extra=log_fields(
                        changeCount=len(patch.changes),
                        droppedChangeCount=sum(dropped.values()),
                        droppedChanges=dropped,
                    ),
                )
            return memory
