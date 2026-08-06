"""User Memory 갱신의 크기 정책과 출력 검사 (#64).

두 방향을 다룬다.

- **입력** — 접수한 하루 기록을 프롬프트에 실을 만큼으로 줄인다. 거절하지 않고
  **자른다**. App Server 는 4xx 를 "미접수 확정" 으로 읽고 앱에 502 를 주므로,
  이벤트가 많은 정상적인 하루가 사용자에게 저장 실패로 보이면 안 된다.
- **출력** — LLM 이 만든 갱신본이 크기·민감정보 규칙을 지켰는지 본다. 여기서는
  **검출만** 하고 고치지 않는다. 압축은 의미 판단이라 코드가 문장을 자르면 뜻이
  달라진다(:mod:`app.services.duration_guard` 와 같은 철학이다).

## 1,000토큰을 문자 수로 재는 이유

이슈가 정한 상한은 "전체 1,000토큰" 인데, 정확한 토큰 수는 tokenizer 종속이다. 이
프로젝트는 OpenAI·Gemini·Bedrock 을 모두 지원하고 tokenizer 의존성이 없다. provider
를 바꿨다고 저장 가능 여부가 달라지면 안 되므로, provider 와 무관한 **직렬화 문자
수**를 정본으로 삼는다.

한국어는 tokenizer 에 따라 1자당 0.6~1.5 토큰이라 여유를 두고 1,200자로 잡았다.
이 값은 이후 Timeline·Question 프롬프트에 **매 요청마다** 실리므로 작을수록 낫다.
표준이 아니라 우리가 고른 값이고, 상수 하나라 언제든 바꿀 수 있다.

필드별 상한 합계(10×200 + 5×150 = 2,750자)는 전체 상한보다 크다. 즉 실질 제약은
전체 상한이고, 필드를 많이 채우면 압축 재요청이 돈다. 의도한 동작이다 — 필드 상한은
한 필드가 전체를 잡아먹지 못하게 하는 방어선이고, 총량은 별도 예산이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.schemas.user_memory import UserMemory
from app.schemas.user_memory_update import DiaryEntry, DiaryEvent
from app.services.notification_guard import SENSITIVE_PATTERNS

#: 갱신본 전체의 직렬화 문자 수 상한(이슈의 1,000토큰을 문자 수로 옮긴 값).
USER_MEMORY_MAX_CHARS = 1_200

#: 프롬프트에 실을 최대 일기 수. 최근 것부터 남긴다.
MAX_DIARY_COUNT = 7

#: 프롬프트에 실을 최대 event 수(요청 전체 합계).
MAX_EVENT_COUNT = 50

#: 사용자가 직접 쓴 메모의 상한. App Server 도 같은 값으로 막지만, 넘겨받은 값을
#: 그대로 믿지 않는다.
MEMO_MAX_CHARS = 500

#: AI 가 쓴 문장(title/subtitle/question)의 상한. 저장 계약과 같은 값이다.
TEXT_MAX_CHARS = 255


@dataclass(frozen=True)
class DiaryDigest:
    """프롬프트에 실을 하루 기록과, 무엇을 얼마나 버렸는지.

    ``stats`` 는 전부 정수라 그대로 운영 이벤트에 실을 수 있다. 본문은 담지 않는다.
    """

    diaries: list[dict[str, Any]]
    stats: dict[str, int]

    @property
    def has_memo(self) -> bool:
        """사용자가 직접 쓴 글이 하나라도 있는가.

        없으면 성향 계열 필드는 갱신할 근거가 없다. 그것은 정상이며 실패가 아니다.
        """

        return self.stats["memoCount"] > 0


def _clip(value: str | None, limit: int) -> str | None:
    """상한을 넘는 문자열을 자른다. 비어 있으면 ``None``.

    프롬프트에 실릴 값이라 자른 흔적(``…``)을 남기지 않는다. 모델이 그 기호를
    문장의 일부로 읽고 따라 쓸 이유가 없다.
    """

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _project_event(event: DiaryEvent) -> dict[str, Any]:
    """event 하나를 프롬프트용 최소 형태로 접는다.

    시각은 분을 버리고 시 단위로만 준다. 갱신 대상은 "몇 시 몇 분에 무엇을 했다" 가
    아니라 **일정 기간 유효한 생활 구조**이고, 분 단위 값은 그 판단에 쓸모가 없으면서
    프로필 문장에 새어 들어갈 위험만 만든다(#61 의 문장 계약과 같은 이유다).
    """

    projected: dict[str, Any] = {
        "eventType": event.event_type,
        "hour": event.start_at.hour,
    }
    title = _clip(event.title, TEXT_MAX_CHARS)
    if title:
        projected["title"] = title
    subtitle = _clip(event.subtitle, TEXT_MAX_CHARS)
    if subtitle:
        projected["subtitle"] = subtitle
    memo = _clip(event.memo, MEMO_MAX_CHARS)
    if memo:
        projected["memo"] = memo
    return projected


def _event_priority(item: tuple[int, int, DiaryEvent]) -> tuple[int, float]:
    """남길 순서를 정하는 키(작을수록 먼저 남긴다).

    **메모가 있는 event 를 끝까지 지킨다.** 사용자가 직접 쓴 글은 성향 계열 필드의
    유일한 근거다. 그것을 먼저 버리면 갱신할 수 있는 것이 AI 가 쓴 문장밖에 남지
    않는다. 같은 조건이면 최근 것을 남긴다.
    """

    _, _, event = item
    has_memo = 0 if (event.memo or "").strip() else 1
    return (has_memo, -event.start_at.timestamp())


def build_diary_digest(diaries: list[DiaryEntry]) -> DiaryDigest:
    """접수한 하루 기록을 프롬프트에 실을 만큼으로 줄인다.

    1. 최근 :data:`MAX_DIARY_COUNT` 일만 남긴다.
    2. 전체 event 가 :data:`MAX_EVENT_COUNT` 를 넘으면, **메모 있는 event 를 모두
       남긴 뒤** 남는 자리를 최근 event 로 채운다.
    3. 남은 것을 날짜 오름차순·시간 오름차순으로 다시 묶는다.

    버린 양은 ``stats`` 에 남는다. 조용히 자르면 결과만 보고는 "다 봤는데 이 정도"
    인지 "못 본 게 있어서 이 정도" 인지 구분할 수 없다.
    """

    ordered = sorted(diaries, key=lambda entry: entry.date, reverse=True)
    kept_diaries = ordered[:MAX_DIARY_COUNT]
    dropped_diary_count = len(ordered) - len(kept_diaries)

    # (일기 index, event index, event) 로 들고 다녀야 잘라낸 뒤 원래 자리로 되돌릴 수 있다.
    flattened = [
        (diary_index, event_index, event)
        for diary_index, entry in enumerate(kept_diaries)
        for event_index, event in enumerate(entry.events)
    ]
    total_event_count = len(flattened)
    memo_count = sum(
        1 for _, _, event in flattened if (event.memo or "").strip()
    )

    kept = sorted(flattened, key=_event_priority)[:MAX_EVENT_COUNT]
    kept.sort(key=lambda item: (item[0], item[1]))
    dropped_event_count = total_event_count - len(kept)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for diary_index, _, event in kept:
        grouped.setdefault(diary_index, []).append(_project_event(event))

    payload: list[dict[str, Any]] = []
    for diary_index, entry in enumerate(kept_diaries):
        events = grouped.get(diary_index, [])
        if not events:
            # event 가 전부 잘려 나간 날은 싣지 않는다. 날짜만 남은 항목은 모델에게
            # "이 날은 아무 일도 없었다" 로 읽힌다.
            continue
        payload.append({"date": entry.date, "events": events})
    payload.sort(key=lambda entry: entry["date"])

    return DiaryDigest(
        diaries=payload,
        stats={
            "diaryCount": len(payload),
            "eventCount": len(kept),
            "memoCount": memo_count,
            "droppedDiaryCount": dropped_diary_count,
            "droppedEventCount": dropped_event_count,
        },
    )


def serialized_chars(memory: UserMemory) -> int:
    """프롬프트에 실릴 형태의 직렬화 문자 수.

    :meth:`~app.schemas.user_memory.UserMemory.prompt_payload` 기준이다. 실제로 토큰을
    쓰는 것이 그 문자열이고, 메타데이터는 프롬프트에 실리지 않는다.
    """

    return len(
        json.dumps(
            memory.prompt_payload(), ensure_ascii=False, separators=(",", ":")
        )
    )


def find_violations(memory: UserMemory) -> list[str]:
    """갱신본이 어긴 규칙을 사람이 읽을 한 줄씩으로 돌려준다(빈 목록이면 통과).

    이 문장은 **재요청 프롬프트와 로그에 그대로 실린다.** 그래서 어느 필드가 어떤
    규칙을 어겼는지까지만 적고 값은 인용하지 않는다 — 민감정보를 지적하면서 그 값을
    같이 남기면 막으려던 것이 로그로 새어 나간다.

    필드별 길이·``customAttributes`` 개수는 Pydantic 이 이미 막았으므로 여기서는
    전체 크기와 민감정보만 본다.
    """

    violations: list[str] = []

    size = serialized_chars(memory)
    if size > USER_MEMORY_MAX_CHARS:
        violations.append(
            f"전체 크기가 {size}자로 상한 {USER_MEMORY_MAX_CHARS}자를 넘었습니다. "
            "중복 표현 제거 → 오래된 단기 관심사 제거 → 영향이 적은 정보 제거 → "
            "문장 병합 → 중요도가 낮은 customAttributes 제거 순으로 줄이세요."
        )

    for field, label in _sensitive_hits(memory):
        violations.append(
            f"`{field}` 에 {label} 형태의 값이 그대로 남아 있습니다. "
            "구체적인 값 대신 해석에 필요한 의미만 남기세요."
        )

    return violations


def _sensitive_hits(memory: UserMemory) -> list[tuple[str, str]]:
    """(필드 이름, 패턴 라벨) 목록. 값은 돌려주지 않는다."""

    dumped = memory.model_dump(by_alias=True)
    texts: list[tuple[str, str]] = [
        (name, value)
        for name, value in dumped.items()
        if isinstance(value, str) and value
    ]
    texts.extend(
        (f"customAttributes.{key}", value)
        for key, value in memory.custom_attributes.items()
        if value
    )

    return [
        (field, label)
        for field, text in texts
        for label, pattern in SENSITIVE_PATTERNS
        if pattern.search(text)
    ]
