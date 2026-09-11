"""Notification Event Agent.

알림에서 얻을 정보는 대화·결제·예약 셋이다(#116). 코드는 그 판단을 대신하지 않는다.
앱이 어떤 정보를 주는지(정책)와 같은 대화방 메시지 묶음처럼 **사실**만 만들어 넘기고,
무엇을 candidate 로 남길지는 알림 내용을 읽은 모델이 정한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.agents.events.base_event_agent import EventAgent
from app.agents.events.notification.app_dictionary import (
    match_policy_ids,
    normalize_app_name,
    policies_for_prompt,
    provides_conversation,
)
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
)
from app.agents.prompt_loader import load_prompt
from app.schemas import AgentEventResult, NotificationItem, TimelineDraftRequest
from app.services.notification_guard import verify_notification_result
from app.services.validator import parse_datetime
from app.core.llm_stages import LLMStage

_SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")


class NotificationEventAgent(EventAgent):
    """알림 source를 해석하는 추론 Agent."""

    name = "notification"
    source_attrs = ("notifications",)

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.NOTIFICATION)
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(getattr(request, "notifications", None) or [])
        if not items:
            return AgentEventResult()

        infer_prompt = build_infer_prompt(
            json.dumps(build_notification_payload(items), ensure_ascii=False, indent=2),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        result = self.llm.complete_structured(
            infer_prompt, AgentEventResult, system=_SYSTEM_PROMPT, temperature=0.2
        )
        return verify_notification_result(result, request)


def build_notification_payload(items: list[NotificationItem]) -> dict:
    """프롬프트에 싣는 알림 입력.

    - ``policies``: 이번에 받은 알림에 걸린 정책만, 본문 한 번씩.
    - ``notifications``: 결제·예약 계열 앱의 알림. 각 알림은 ``policyIds`` 로 정책을 가리킨다.
    - ``conversations``: 정책이 없거나 대화를 주는 앱(카카오톡)의 알림을 앱과 대화 상대
      (``title``) 단위로 묶은 것. 메시지 수가 많은 순서다. 단체방 이름은 입력에 없어
      방 단위로는 묶지 않는다.

    알림은 한 건도 버리지 않는다. 모든 rawId 가 두 목록 중 한 곳에 한 번씩 있다.
    """

    notifications: list[dict] = []
    conversation_items: list[NotificationItem] = []
    used_policy_ids: list[str] = []

    for item in items:
        policy_ids = match_policy_ids(item.app_name)
        used_policy_ids.extend(pid for pid in policy_ids if pid not in used_policy_ids)
        if not policy_ids or provides_conversation(policy_ids):
            conversation_items.append(item)
            continue
        entry = item.model_dump(by_alias=True, mode="json")
        entry["policyIds"] = list(policy_ids)
        notifications.append(entry)

    return {
        "policies": policies_for_prompt(used_policy_ids),
        "notifications": notifications,
        "conversations": _conversations(conversation_items),
    }


def _conversations(items: list[NotificationItem]) -> list[dict]:
    """같은 앱·같은 ``title``(대화 상대)의 알림을 하나로 묶는다."""

    groups: dict[tuple[str, str], list[NotificationItem]] = {}
    for item in items:
        key = (normalize_app_name(item.app_name), item.title.strip())
        groups.setdefault(key, []).append(item)

    conversations = []
    for members in groups.values():
        members.sort(key=lambda m: _posted_sort_value(m.posted_at))
        first = members[0]
        conversations.append(
            {
                "appName": first.app_name,
                "title": first.title.strip(),
                "policyIds": list(match_policy_ids(first.app_name)),
                "messageCount": len(members),
                "firstPostedAt": first.posted_at,
                "lastPostedAt": members[-1].posted_at,
                # 메시지 사이 최대 간격. 크면 이어진 대화가 아니다 — 하나의 긴 구간으로
                # 묶으면 그 사이의 다른 활동이 대화에 덮인다(#56 §5.4).
                "maxGapMinutes": _max_gap_minutes([m.posted_at for m in members]),
                "messages": [
                    {"rawId": m.raw_id, "postedAt": m.posted_at, "text": m.text}
                    for m in members
                ],
            }
        )

    # 대화 내용이 많을수록 중요하다(#116). 같으면 먼저 시작한 대화가 앞이다.
    conversations.sort(
        key=lambda c: (-c["messageCount"], _posted_sort_value(c["firstPostedAt"]))
    )
    return conversations


def _posted_sort_value(posted_at: str) -> tuple[int, datetime | str]:
    """시각으로 읽히면 시각으로, 아니면 원문 순서 뒤에 둔다."""

    parsed = parse_datetime(posted_at, timezone.utc)
    return (0, parsed) if parsed is not None else (1, posted_at)


def _max_gap_minutes(posted_at: list[str]) -> float | None:
    """수신 시각들 사이의 최대 간격(분). 계산할 수 없으면 None."""

    parsed = [
        dt
        for value in posted_at
        if (dt := parse_datetime(value, timezone.utc)) is not None
    ]
    if len(parsed) < 2:
        return None
    parsed.sort()
    return max(
        (later - earlier).total_seconds() / 60.0
        for earlier, later in zip(parsed, parsed[1:])
    )
