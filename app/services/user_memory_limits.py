"""User Memory 갱신의 크기 정책과 출력 검사 (#64).

두 방향을 다룬다.

- **입력** — 접수 schema 가 하루 타임라인을 최대 5건으로 제한한다. 그 안의 event와
  본문은 프롬프트에 실을 만큼으로 줄이며, event가 많은 정상적인 하루는 거절하지 않고
  **자른다**. 사용자가 고른 하루 감정은 날짜 항목에 그대로 싣는다(#121).
- **출력** — LLM 이 만든 갱신본이 크기·민감정보 규칙을 지켰는지 본다. 여기서는
  **검출만** 하고 고치지 않는다. 압축은 의미 판단이라 코드가 문장을 자르면 뜻이
  달라진다(:mod:`app.services.duration_guard` 와 같은 철학이다).

## 전체 크기를 문자 수로 재는 이유

정확한 토큰 수는 tokenizer 종속이다. 이 프로젝트는 OpenAI·Gemini·Bedrock 을 모두
지원하고 tokenizer 의존성이 없다. provider 를 바꿨다고 저장 가능 여부가 달라지면 안
되므로, provider 와 무관한 **직렬화 문자 수**를 정본으로 삼는다.

## 전체 상한이 하는 일 (#121)

처음(#64)에는 1,200자였고 "짧게 눌러 담는 예산" 이었다. 지금은 2,000자이고 **문서가
끝없이 커지는 것을 막는 선**이다. 갱신 정책이 "한 번 나온 정보도 남긴다" 로 바뀌고
``customAttributes`` 개수 제한이 없어져, 끝을 막는 값이 이것 하나뿐이다.

없앨 수 없는 이유는 이 문서가 **매 요청마다 통째로 읽히기** 때문이다. Timeline·
Question·Repair(v3) 프롬프트와 갱신 요청 자신이 문서 전체를 싣는다. 개수 제한이 없는
``customAttributes`` 가 끝없이 자라면 그 비용을 모든 요청이 낸다. v1·v2 세트는 여기에
더해 갱신 때 문서 전체를 다시 출력하므로, 문서가 출력 한도를 넘으면 갱신 자체가 실패한다.

상한을 넘은 갱신본은 저장되지 않고 기존 문서가 그대로 남는다. 다시 요청하지 않으므로
**1차 출력이 상한 안에 드는 것**이 전부다.

2,000자는 표준이 아니라 우리가 고른 값이다.

필드별 상한 합계(10×500자)는 전체 상한보다 크다. 즉 실질 제약은 전체 상한이다. 필드
상한은 한 필드가 전체를 잡아먹지 못하게 하는 방어선이고, 총량은 별도 예산이다.

## 상한과 목표가 따로 있다

거절 기준(:data:`USER_MEMORY_MAX_CHARS`)과 모델에게 알려 주는 목표
(:data:`USER_MEMORY_TARGET_CHARS`)는 다른 값이다. 보존 정책 아래에서 프로필은 며칠이면
상한에 닿고 그 뒤로는 **매일** 상한 근처에서 갱신된다. 그 구간에서 갱신이 안정적으로
통과하려면 모델이 겨냥하는 값이 상한보다 낮아야 한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.schemas.user_memory import UserMemory
from app.schemas.user_memory_update import (
    MAX_DAILY_TIMELINE_COUNT,
    DailyTimeline,
    DailyTimelineEvent,
)
from app.services.notification_guard import SENSITIVE_PATTERNS

#: 갱신본 전체의 직렬화 문자 수 상한(#121 에서 1,200 → 2,000).
USER_MEMORY_MAX_CHARS = 2_000

#: 모델에게 **목표로 알려 주는** 크기. 거절 기준은 여전히 :data:`USER_MEMORY_MAX_CHARS` 다.
#:
#: 둘을 가른 것은 모델이 글자 수를 세지 못하기 때문이다. 상한에 닿은 프로필에 하루씩
#: 갱신을 얹어 실측했을 때(#121), 상한만 알려 주면 모델은 2,010~2,140자를 냈고 재요청
#: 세 번을 다 쓰고도 통과하지 못했다. 통과하지 못하면 프로필이 그대로 남아 다음 날도
#: 같은 자리에서 시작하므로 **한 번의 실패가 매일 반복된다** — 닷새 중 나흘이 실패했다.
#:
#: 그래서 모델에게는 상한보다 낮은 값을 겨냥하게 하고, 넘치는 몫은 상한과의 사이에서
#: 받아 낸다. 저장되는 프로필은 대개 이 값과 상한 사이에 있다.
#:
#: 1,600(상한의 80%)은 표준이 아니라 실측으로 고른 값이다.
USER_MEMORY_TARGET_CHARS = 1_600

#: 하루당 최대 event 수. **날짜별 몫이다.**
#:
#: 전체 상한 하나만 두면 event 가 몰린 하루가 다른 날의 자리를 다 먹는다. 정렬 기준이
#: (메모 있음, 최근순)이라 오래된 날이 통째로 밀려나고, event 가 하나도 안 남은 날은
#: payload 에서 빠져 모델에게는 애초에 없던 날이 된다.
#:
#: 10 은 Timeline v3 가 하루를 구성하는 event 의 최대 개수다(#119 에서 24 → 10). 하루 기록이
#: 그만큼으로 구성되므로 갱신에 싣는 몫도 같은 값이다. 이보다 많은 날(v1·v2 세트가 만든
#: 하루, 사용자가 손으로 event 를 더한 날)은 memo 있는 event 를 먼저, 그다음 최근 것을 남긴다.
MAX_EVENTS_PER_TIMELINE = 10

#: 요청 전체 event 상한. 날짜별 몫에서 파생되므로 따로 정하지 않는다.
MAX_EVENT_COUNT = MAX_DAILY_TIMELINE_COUNT * MAX_EVENTS_PER_TIMELINE

#: 사용자가 직접 쓴 메모의 상한. App Server 도 같은 값으로 막지만, 넘겨받은 값을
#: 그대로 믿지 않는다.
MEMO_MAX_CHARS = 500

#: AI 가 쓴 문장(title/subtitle/question)의 상한. 저장 계약과 같은 값이다.
TEXT_MAX_CHARS = 255

#: 하루 감정 값의 상한. App Server 가 이 값을 담는 컬럼 길이와 같다. 값은 enum 이름
#: (``HAPPY`` 등)이라 넘을 일이 없지만, 넘겨받은 값을 그대로 믿지 않는다.
EMOTION_MAX_CHARS = 32

#: 문장의 끝. 마침표·물음표·느낌표 뒤에 공백이 오는 자리에서 가른다.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class DailyTimelineDigest:
    """프롬프트에 실을 하루 타임라인과, 무엇을 얼마나 버렸는지.

    ``stats`` 는 전부 정수라 그대로 운영 이벤트에 실을 수 있다. 본문은 담지 않는다.
    """

    daily_timelines: list[dict[str, Any]]
    stats: dict[str, int]

    @property
    def has_memo(self) -> bool:
        """사용자가 직접 쓴 글이 하나라도 있는가.

        이 값으로 동작이 갈리는 것은 v1·v2 프롬프트 세트뿐이다. 거기서는 ``memo`` 가
        없으면 성향 계열 필드를 갱신할 근거가 없고, 그것은 정상이며 실패가 아니다.
        v3 는 AI 가 쓴 문장도 근거로 읽으므로 이 값을 보지 않는다(#121).
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


def _project_event(event: DailyTimelineEvent) -> dict[str, Any]:
    """event 하나를 프롬프트용 최소 형태로 접는다.

    시각은 분을 버리고 시 단위로만 준다. 프로필에 남길 것은 "몇 시 몇 분에 무엇을
    했다" 가 아니라 **언제쯤 무엇을 하는 사람인가**이고, 분 단위 값은 그 판단에 쓸모가
    없으면서 프로필 문장에 새어 들어갈 위험만 만든다(#61 의 문장 계약과 같은 이유다).

    끝 시각도 같은 단위로 준다(#121). 시작만 있으면 그 일을 얼마 동안 했는지 알 수
    없어, 근무 시간이나 저녁을 보내는 방식 같은 것을 읽을 수 없다. 자정을 넘긴 event 는
    ``endHour`` 가 ``hour`` 보다 작다.
    """

    projected: dict[str, Any] = {
        "eventType": event.event_type,
        "hour": event.start_at.hour,
    }
    if event.end_at is not None:
        projected["endHour"] = event.end_at.hour
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


def _event_priority(item: tuple[int, int, DailyTimelineEvent]) -> tuple[int, float]:
    """남길 순서를 정하는 키(작을수록 먼저 남긴다).

    **메모가 있는 event 를 끝까지 지킨다.** event 안에서 사용자가 직접 쓴 글은 이것
    하나다. 그것을 먼저 버리면 남는 것이 AI 가 쓴 문장뿐이고, 둘이 어긋날 때 무엇이
    맞는지 가릴 근거가 사라진다. 같은 조건이면 최근 것을 남긴다.
    """

    _, _, event = item
    has_memo = 0 if (event.memo or "").strip() else 1
    return (has_memo, -event.start_at.timestamp())


def build_daily_timeline_digest(daily_timelines: list[DailyTimeline]) -> DailyTimelineDigest:
    """접수한 하루 타임라인을 프롬프트에 실을 만큼으로 줄인다.

    1. 최근 :data:`MAX_DAILY_TIMELINE_COUNT` 일만 남긴다.
    2. **하루마다** :data:`MAX_EVENTS_PER_TIMELINE` 개까지 남긴다. 메모 있는 event 를
       먼저 남기고 남는 자리를 최근 event 로 채운다.
    3. 남은 것을 날짜 오름차순·시간 오름차순으로 다시 묶는다.

    2번을 날짜별로 하는 것이 핵심이다. 전체 상한 하나로 자르면 event 가 몰린 하루가
    다른 날의 자리를 다 먹고, 밀려난 날은 payload 에서 빠져 모델에게는 애초에 없던
    날이 된다.

    버린 양은 ``stats`` 에 남는다. 조용히 자르면 결과만 보고는 "다 봤는데 이 정도"
    인지 "못 본 게 있어서 이 정도" 인지 구분할 수 없다.
    """

    ordered = sorted(
        daily_timelines, key=lambda entry: entry.record_date, reverse=True
    )
    kept_timelines = ordered[:MAX_DAILY_TIMELINE_COUNT]
    dropped_timeline_count = len(ordered) - len(kept_timelines)

    # (일기 index, event index, event) 로 들고 다녀야 잘라낸 뒤 원래 자리로 되돌릴 수 있다.
    flattened = [
        (timeline_index, event_index, event)
        for timeline_index, entry in enumerate(kept_timelines)
        for event_index, event in enumerate(entry.events)
    ]
    total_event_count = len(flattened)
    memo_count = sum(
        1 for _, _, event in flattened if (event.memo or "").strip()
    )

    kept: list[tuple[int, int, DailyTimelineEvent]] = []
    for timeline_index in range(len(kept_timelines)):
        same_day = [item for item in flattened if item[0] == timeline_index]
        kept.extend(
            sorted(same_day, key=_event_priority)[:MAX_EVENTS_PER_TIMELINE]
        )
    kept.sort(key=lambda item: (item[0], item[1]))
    dropped_event_count = total_event_count - len(kept)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for timeline_index, _, event in kept:
        grouped.setdefault(timeline_index, []).append(_project_event(event))

    payload: list[dict[str, Any]] = []
    for timeline_index, entry in enumerate(kept_timelines):
        events = grouped.get(timeline_index, [])
        if not events:
            # event 가 전부 잘려 나간 날은 싣지 않는다. 날짜만 남은 항목은 모델에게
            # "이 날은 아무 일도 없었다" 로 읽힌다.
            continue
        # ``date`` 는 Agent 프롬프트용 내부 digest 키다. 공개 접수 계약의
        # ``recordDate`` 와 분리해 프롬프트 입력을 불필요하게 바꾸지 않는다.
        projected: dict[str, Any] = {"date": entry.record_date}
        emotion = _clip(entry.emotion_type, EMOTION_MAX_CHARS)
        if emotion:
            # 사용자가 하루를 저장하며 **직접 고른** 값이다(#121). ``memo`` 와 함께
            # 입력에서 사용자가 남긴 둘뿐인 신호다. 값의 뜻은 프롬프트가 설명한다 —
            # 여기서 풀어 쓰면 App Server 가 값을 더했을 때 코드가 그것을 모른다.
            projected["emotion"] = emotion
        projected["events"] = events
        payload.append(projected)
    payload.sort(key=lambda entry: entry["date"])

    return DailyTimelineDigest(
        daily_timelines=payload,
        stats={
            "dailyTimelineCount": len(payload),
            "eventCount": len(kept),
            "memoCount": memo_count,
            "emotionCount": sum(1 for entry in payload if "emotion" in entry),
            "droppedDailyTimelineCount": dropped_timeline_count,
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


def _sentence_count(text: str) -> int:
    """문장 수. 끝맺음 부호가 없는 글도 한 문장으로 센다."""

    return len([part for part in _SENTENCE_BOUNDARY.split(text.strip()) if part])


def _prompt_items(memory: UserMemory) -> list[tuple[str, str]]:
    """프롬프트에 실리는 (항목 이름, 값) 목록. 고정 필드 다음에 ``customAttributes`` 다.

    값은 크기를 재는 데만 쓴다. 이 모듈이 내보내는 문장에 값을 싣지 않는다.
    """

    payload = memory.prompt_payload()
    items = [
        (name, value) for name, value in payload.items() if name != "customAttributes"
    ]
    items.extend(
        (f"customAttributes.{key}", value)
        for key, value in payload.get("customAttributes", {}).items()
    )
    return items


def shrink_budget(
    memory: UserMemory, *, target_chars: int = USER_MEMORY_TARGET_CHARS
) -> list[str]:
    """문서를 목표 크기에 맞추려면 항목마다 몇 문장까지 쓸 수 있는지(#121).

    목표 안이면 빈 목록이다. 돌려주는 줄에는 항목 이름과 숫자만 있고 값은 없다.

    **글자 수가 아니라 문장 수로 말한다.** 모델은 글자 수를 세지 못한다. 상한을 넘은
    같은 문서를 두고 지시 형태만 바꿔 실측했을 때, "전체 N자 줄여라" 는 1% 가, 항목별
    글자 수는 5% 가, 항목별 문장 수는 14% 가 줄었다. 앞의 둘로는 재요청을 다 써도
    상한 아래로 내려오지 못했다.

    몫은 지금 문장 수에 (목표 ÷ 현재 크기)를 곱해 내림한 값이고 1보다 작아지지 않는다.
    내림이라 두 문장 이상인 항목은 적어도 한 문장이 준다. **무엇을 줄일지는 정하지
    않는다** — 어느 문장을 남길지는 의미 판단이고 프롬프트 세트의 정책이다.

    한 문장짜리 항목은 문장 수로 줄일 수 없다. 그런 항목이 많아 문장 수를 맞춰도 목표를
    넘으면 ``customAttributes`` 항목 수의 몫을 함께 준다. 개수 제한이 없는 자리라 문서가
    커지는 쪽은 대개 여기다.
    """

    size = serialized_chars(memory)
    if size <= target_chars:
        return []

    lines: list[str] = []
    estimated = size
    for name, value in _prompt_items(memory):
        count = _sentence_count(value)
        allowed = max(1, count * target_chars // size)
        estimated -= len(value) * (count - allowed) // count
        lines.append(f"`{name}`: 지금 {count}문장 → {allowed}문장 이내")

    if estimated > target_chars:
        attribute_count = len(memory.prompt_payload().get("customAttributes", {}))
        keep = attribute_count * target_chars // estimated
        if 0 < keep < attribute_count:
            lines.append(
                f"`customAttributes` 항목 수: 지금 {attribute_count}개 → {keep}개 이내"
            )
        lines.append("문장 수를 맞춰도 목표를 넘습니다. 남긴 문장도 짧게 다시 쓰세요.")

    return lines


def find_violations(memory: UserMemory) -> list[str]:
    """갱신본이 어긴 규칙을 사람이 읽을 한 줄씩으로 돌려준다(빈 목록이면 통과).

    이 문장은 **재요청 프롬프트와 로그에 그대로 실린다.** 그래서 어느 필드가 어떤
    규칙을 어겼는지까지만 적고 값은 인용하지 않는다 — 민감정보를 지적하면서 그 값을
    같이 남기면 막으려던 것이 로그로 새어 나간다.

    필드별 길이는 Pydantic 이 이미 막았으므로 여기서는 전체 크기와 민감정보만 본다.

    **무엇부터 줄일지는 여기 적지 않는다**(#121). 그것은 갱신 정책이고 정책은 프롬프트
    세트가 갖는다 — 무엇을 남기는지가 버전마다 다른데 줄이는 순서를 코드에 박아 두면
    재요청 때 시스템 프롬프트와 다른 말을 하게 된다.

    **얼마나 줄일지는 적는다.** 그것은 셀 수 있는 값이고 모델이 스스로 세지 못하는
    값이다. 항목마다 문장 수로 준다(:func:`shrink_budget`).
    """

    violations: list[str] = []

    size = serialized_chars(memory)
    if size > USER_MEMORY_MAX_CHARS:
        budget = "\n".join(f"  - {line}" for line in shrink_budget(memory))
        violations.append(
            f"전체 크기가 {size}자로 상한 {USER_MEMORY_MAX_CHARS}자를 넘었습니다. "
            f"목표 크기 {USER_MEMORY_TARGET_CHARS}자에 맞도록 아래 항목마다 적힌 "
            "문장 수 이내로 다시 쓰세요. 무엇부터 줄일지는 시스템 프롬프트의 「크기」 "
            f"절을 따릅니다.\n{budget}"
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
