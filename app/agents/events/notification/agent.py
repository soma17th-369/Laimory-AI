"""Notification Event Agent."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.agents.events.base_event_agent import EventAgent
from app.agents.events.notification.app_dictionary import (
    app_dictionary_for_prompt,
    enrich_notification_item,
)
from app.agents.parsing import (
    SupportsComplete,
    build_infer_prompt,
    default_llm,
    user_memory_to_text,
)
from app.schemas import AgentEventResult, TimelineDraftRequest

_SYSTEM_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")

_DIRECT_MEETING_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"만나",
        r"볼래",
        r"보자",
        r"몇\s*시",
        r"어디서",
        r"약속",
        r"번개",
        r"모이",
        r"집합",
    )
]
_PLACE_MEETING_PATTERN = re.compile(
    r"(?P<place>[가-힣A-Za-z0-9·.\-\s]{2,30}?)(?:에서|앞에서|근처에서)\s*(?:보자|볼래|만나)"
)
_LOW_PRIORITY_KEYWORDS = (
    "뉴스",
    "유튜브",
    "youtube",
    "검색",
    "광고",
    "프로모션",
    "혜택",
    "이벤트",
    "추천",
)
_CONTEXT_ONLY_KEYWORDS = ("ssafy", "싸피", "설문", "채용설명회", "공지")


class NotificationEventAgent(EventAgent):
    """알림 source를 해석하는 추론 Agent."""

    name = "notification"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm()
        return self._llm

    def _generate(self, request: TimelineDraftRequest) -> AgentEventResult:
        items = list(getattr(request, "notifications", None) or [])
        if not items:
            return AgentEventResult()

        data_text = _notification_items_to_text(items)
        infer_prompt = build_infer_prompt(
            user_memory_to_text(request.user_memory),
            data_text,
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        return self.llm.complete_structured(
            infer_prompt, AgentEventResult, system=_SYSTEM_PROMPT, temperature=0.2
        )


def _notification_items_to_text(items: list) -> str:
    enriched_items = [enrich_notification_item(item) for item in items]
    _annotate_notification_use(enriched_items)
    payload = {
        "appDictionary": app_dictionary_for_prompt(),
        "messengerAnalysis": _messenger_analysis(enriched_items),
        "notifications": enriched_items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _annotate_notification_use(items: list[dict]) -> None:
    for item in items:
        policy = item.get("appPolicy") or {}
        category = policy.get("category")
        searchable = _searchable_text(item)
        content_text = (item.get("text") or "").casefold()
        direct_meeting = _has_direct_meeting_mention(searchable)
        explicit_place = _extract_meeting_place(content_text)

        is_context_only = category == "CONTEXT" or _has_any_keyword(
            searchable, _CONTEXT_ONLY_KEYWORDS
        )
        is_low_priority = category in {"UNKNOWN", "LOW_PRIORITY"} or _has_any_keyword(
            searchable, _LOW_PRIORITY_KEYWORDS
        )

        item["timelineUseGuidance"] = {
            "category": category or "UNKNOWN",
            "allowCandidateFromNotificationOnly": category
            in {"PAYMENT", "SCHEDULE"}
            or (category == "MESSENGER" and direct_meeting),
            "requiresOtherSourcesForSchedule": category
            not in {"PAYMENT", "SCHEDULE"}
            and not direct_meeting,
            "shouldNotCreateTimelineEvent": is_low_priority and not direct_meeting,
            "contextOnly": is_context_only,
            "lowConfidenceFragmentOnly": is_low_priority or is_context_only,
            "placeLabelAllowedFromNotification": bool(explicit_place and direct_meeting),
            "explicitPlaceMention": explicit_place,
        }


def _messenger_analysis(items: list[dict]) -> dict:
    conversations: dict[str, dict] = {}
    for item in items:
        policy = item.get("appPolicy") or {}
        if policy.get("category") != "MESSENGER":
            continue

        speaker = (item.get("title") or item.get("detectedAppName") or "").strip()
        content = (item.get("text") or "").strip()
        key = speaker or "unknown"
        conversation = conversations.setdefault(
            key,
            {
                "speaker": speaker,
                "count": 0,
                "rawIds": [],
                "hasDirectMeetingMention": False,
                "explicitPlaceMention": None,
                "placeLabelAllowed": False,
                "interpretation": "",
            },
        )
        conversation["count"] += 1
        if item.get("rawId"):
            conversation["rawIds"].append(item["rawId"])

        direct_meeting = _has_direct_meeting_mention(content)
        explicit_place = _extract_meeting_place(content)
        if direct_meeting:
            conversation["hasDirectMeetingMention"] = True
        if explicit_place and direct_meeting:
            conversation["explicitPlaceMention"] = explicit_place
            conversation["placeLabelAllowed"] = True

        item["messengerInterpretation"] = {
            "speakerField": "title",
            "contentField": "text",
            "speaker": speaker,
            "content": content,
            "isSingleOrSparse": conversation["count"] <= 2,
            "hasDirectMeetingMention": conversation["hasDirectMeetingMention"],
            "explicitPlaceMention": explicit_place if direct_meeting else None,
            "placeLabelAllowed": bool(explicit_place and direct_meeting),
        }

    for conversation in conversations.values():
        if conversation["hasDirectMeetingMention"]:
            conversation["interpretation"] = (
                "직접적인 만남/시간/장소 언급이 있어 약속 단서로 사용할 수 있다."
            )
        elif conversation["count"] >= 3:
            conversation["interpretation"] = (
                "반복 등장한 대화 상대이므로 가까운 사람과의 대화 흐름으로 요약한다."
            )
        else:
            conversation["interpretation"] = (
                "한두 개의 메신저 알림은 일정 후보가 아니라 fragment 또는 무시 대상으로 둔다."
            )

    return {"conversations": list(conversations.values())}


def _searchable_text(item: dict) -> str:
    values = (
        item.get("detectedAppName"),
        item.get("appName"),
        item.get("title"),
        item.get("text"),
    )
    return " ".join(value for value in values if value).casefold()


def _has_direct_meeting_mention(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DIRECT_MEETING_PATTERNS)


def _extract_meeting_place(text: str) -> str | None:
    match = _PLACE_MEETING_PATTERN.search(text)
    if match is None:
        return None
    return match.group("place").strip()


def _has_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.casefold() in text for keyword in keywords)
