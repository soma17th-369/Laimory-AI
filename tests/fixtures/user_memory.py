"""User Memory 갱신(#64) 요청·응답 빌더.

접수 body 를 손으로 쓰면 테스트마다 필드가 조금씩 달라져, 나중에 계약이 바뀌었을 때
어디를 고쳐야 하는지 알 수 없다. 여기 하나만 고치면 되게 한다.
"""

from __future__ import annotations

import json
from typing import Any

from app.schemas.user_memory_update import UserMemoryUpdateRequest

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


def diary_event(
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


def diary(
    *,
    date: str = "2026-08-04",
    record_time_zone: str = "Asia/Seoul",
    emotion_type: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "date": date,
        "recordTimeZone": record_time_zone,
        "emotionType": emotion_type,
        "events": events if events is not None else [diary_event()],
    }


def update_body(**overrides: Any) -> dict[str, Any]:
    """``POST /v1/user-memory`` 접수 body."""

    body: dict[str, Any] = {
        "taskId": TASK_ID,
        "taskToken": TASK_TOKEN,
        "userMemory": None,
        "diaries": [diary()],
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
