"""User Memory 갱신(#64) 요청·응답 빌더.

접수 body 를 손으로 쓰면 테스트마다 필드가 조금씩 달라져, 나중에 계약이 바뀌었을 때
어디를 고쳐야 하는지 알 수 없다. 여기 하나만 고치면 되게 한다.
"""

from __future__ import annotations

import json
from typing import Any

from app.schemas.user_memory import (
    CUSTOM_ATTRIBUTE_MAX_LENGTH,
    NARRATIVE_MAX_LENGTH,
    UserMemory,
)
from app.schemas.user_memory_update import UserMemoryUpdateRequest
from app.services.user_memory_limits import USER_MEMORY_MAX_CHARS, serialized_chars

TASK_ID = "task-user-memory-1"
TASK_TOKEN = "user-memory-token-1"

#: 자연어 필드 alias. LLM 응답을 만들 때 빠짐없이 채우려고 쓴다.
NARRATIVE_FIELDS = (
    "basicProfile",
    "lifeContext",
    "relationships",
    "personality",
    "values",
    "preferences",
    "routines",
    "currentFocus",
    "emotionalPatterns",
    "memoryStyle",
)


def daily_timeline_event(
    *,
    event_type: str = "MEAL",
    title: str = "점심을 먹었어요",
    subtitle: str | None = None,
    question: str | None = None,
    memo: str | None = None,
    start_at: str = "2026-08-04T12:10:00+09:00",
    end_at: str | None = "2026-08-04T13:00:00+09:00",
) -> dict[str, Any]:
    return {
        "eventType": event_type,
        "title": title,
        "subtitle": subtitle,
        "question": question,
        "memo": memo,
        "startAt": start_at,
        "endAt": end_at,
    }


def daily_timeline(
    *,
    record_date: str = "2026-08-04",
    record_time_zone: str = "Asia/Seoul",
    emotion_type: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "recordDate": record_date,
        "recordTimeZone": record_time_zone,
        "emotionType": emotion_type,
        "events": events if events is not None else [daily_timeline_event()],
    }


def update_body(**overrides: Any) -> dict[str, Any]:
    """``POST /v1/user-memory`` 접수 body."""

    body: dict[str, Any] = {
        "taskId": TASK_ID,
        "taskToken": TASK_TOKEN,
        "userMemory": None,
        "dailyTimelines": [daily_timeline()],
    }
    body.update(overrides)
    return body


def update_request(**overrides: Any) -> UserMemoryUpdateRequest:
    return UserMemoryUpdateRequest.model_validate(update_body(**overrides))


def memory_body(**fields: Any) -> dict[str, Any]:
    """v1.0 User Memory dict. 지정하지 않은 자연어 필드는 빈 문자열이다."""

    body: dict[str, Any] = {name: "" for name in NARRATIVE_FIELDS}
    body["customAttributes"] = {}
    body.update(fields)
    return body


def memory_json(**fields: Any) -> str:
    """갱신 Agent 가 돌려줄 LLM 응답(JSON 문자열)."""

    return json.dumps(memory_body(**fields), ensure_ascii=False)


def change(item: str, action: str = "수정", text: str | None = None) -> dict[str, Any]:
    """변경 한 건(#121). ``item`` 은 고정 필드 이름이거나 ``customAttributes.<키>`` 다."""

    return {"item": item, "action": action, "text": text}


def changes_json(*changes: dict[str, Any]) -> str:
    """v3 세트의 갱신 Agent 가 돌려줄 LLM 응답 — 변경 목록(JSON 문자열)."""

    return json.dumps({"changes": list(changes)}, ensure_ascii=False)


#: :func:`profile_with_room` 이 가득 채우는 고정 필드. 나머지 셋(values·preferences·
#: routines)은 비워 둔다 — 테스트가 그 자리에 새 내용을 더한다.
_FILLED_FIELDS = (
    "basic_profile",
    "life_context",
    "relationships",
    "personality",
    "current_focus",
    "emotional_patterns",
    "memory_style",
)


def profile_with_room(room: int, **fields: str) -> UserMemory:
    """전체 상한까지 정확히 ``room`` 자가 남은 문서.

    크기를 숫자로 적어 두면 상한이 바뀔 때마다 테스트를 다시 계산해야 한다. 상한에서
    거꾸로 계산해 만든다. ``fields`` 로 준 필드는 그 값으로 두고, 모자라는 크기는
    ``customAttributes`` 로 채운다.
    """

    values = {name: "가" * NARRATIVE_MAX_LENGTH for name in _FILLED_FIELDS}
    values.update(fields)
    goal = USER_MEMORY_MAX_CHARS - room
    # 마지막 속성이 남은 몇 글자를 받아 낼 수 있게 조금 덜 채운다.
    chunk = CUSTOM_ATTRIBUTE_MAX_LENGTH - 50
    attributes: dict[str, str] = {}

    def size() -> int:
        return serialized_chars(UserMemory(**values, custom_attributes=attributes))

    assert size() <= goal, "고정 필드만으로 이미 그 크기를 넘습니다."
    while size() < goal:
        key = f"채움{len(attributes)}"
        attributes[key] = "나"
        gap = goal - size()
        if gap < 0:
            # 새 속성을 넣을 자리도 없다. 직전 속성을 늘려 맞춘다.
            del attributes[key]
            last = next(reversed(attributes))
            attributes[last] += "나" * (goal - size())
            break
        attributes[key] += "나" * min(chunk - 1, gap)

    memory = UserMemory(**values, custom_attributes=attributes)
    assert serialized_chars(memory) == goal
    return memory
