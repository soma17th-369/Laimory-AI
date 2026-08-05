"""Notification app dictionary loader and matcher.

사전은 "이 앱의 알림을 타임라인에서 어떻게 다뤄야 하는가"를 정책으로 적어 둔 것이다.
정책 하나가 잘못 붙으면 `timelineUseGuidance` 가 `shouldNotCreateTimelineEvent` 를
달아 알림이 조용히 사라진다. 그래서 매칭은 넓게가 아니라 정확하게 한다(#67).

매칭 규칙:

    1. `appName` 을 먼저 본다. 알림이 어느 앱에서 왔는지는 여기에 적혀 있다.
    2. `appName` 에서 못 찾을 때만 `title`, `text` 로 내려간다. 수집기가 실제 앱 이름을
       본문에 넣는 경우가 있어 남겨 둔 경로다.
    3. 같은 필드에서 여러 alias 가 걸리면 **가장 긴 alias** 가 이긴다. 예전에는 정책
       목록의 순서가 유일한 우선순위였는데, 그러면 사전이 커질수록 나빠진다.

`사용`·`취소`·`공지`·`추천` 같은 어느 앱 알림에나 나오는 일반 단어는 alias 에서 뺐다.
결제인지 예약인지의 의미 판단은 alias 가 아니라 정책 본문과 `timelineUseGuidance` 가
담당한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field

from app.schemas import CamelModel, NotificationItem

_DICTIONARY_PATH = Path(__file__).with_name("app_dictionary.json")

#: alias 를 찾을 필드 순서. 앞이 이긴다.
_MATCH_FIELDS = ("appName", "title", "text")


class NotificationAppPolicy(CamelModel):
    """How one app family should influence notification event extraction."""

    key: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    category: str = Field(min_length=1)
    timeline_use: str = Field(alias="timelineUse", min_length=1)
    event_creation: str = Field(alias="eventCreation", min_length=1)
    confidence_hint: str = Field(alias="confidenceHint", min_length=1)
    title_meaning: str | None = Field(default=None, alias="titleMeaning")
    content_meaning: str | None = Field(default=None, alias="contentMeaning")
    user_speech_rule: str | None = Field(default=None, alias="userSpeechRule")

    def to_prompt_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class NotificationAppDictionary(CamelModel):
    """Editable notification app dictionary loaded from JSON."""

    policies: list[NotificationAppPolicy]
    fallback_policy: NotificationAppPolicy = Field(alias="fallbackPolicy")


@dataclass(frozen=True)
class PolicyMatch:
    """어떤 정책이 어디서 왜 붙었는지.

    `field` 와 `alias` 는 진단용이다. 알림이 억제될 때 "무엇 때문에 그 정책이
    붙었는가"를 되짚을 수 있어야 한다.
    """

    policy: NotificationAppPolicy
    field: str | None = None
    alias: str | None = None

    @property
    def is_fallback(self) -> bool:
        return self.field is None

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "policyKey": self.policy.key,
            "matchedField": self.field or "fallback",
            "matchedAlias": self.alias,
        }


@lru_cache(maxsize=1)
def load_app_dictionary() -> NotificationAppDictionary:
    """Load the editable app dictionary file once per process."""

    raw = json.loads(_DICTIONARY_PATH.read_text(encoding="utf-8"))
    return NotificationAppDictionary.model_validate(raw)


def app_dictionary_for_prompt(
    policy_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """프롬프트에 실을 정책 목록.

    `policy_keys` 를 주면 **이번 요청에서 실제로 매치된 정책만** 싣는다(#67). 사전
    전체는 7천 자가 넘어, 알림 두세 건짜리 하루에도 등장하지 않은 앱의 정책까지
    보내게 된다. fallback 정책은 `UNKNOWN` 의 의미를 설명하므로 항상 포함한다.
    """

    dictionary = load_app_dictionary()
    if policy_keys is None:
        return [policy.to_prompt_dict() for policy in dictionary.policies]

    selected = [
        policy for policy in dictionary.policies if policy.key in policy_keys
    ]
    if dictionary.fallback_policy.key not in {policy.key for policy in selected}:
        selected.append(dictionary.fallback_policy)
    return [policy.to_prompt_dict() for policy in selected]


def enrich_notification_item(item: NotificationItem) -> dict[str, Any]:
    """Attach matched app policy to a normalized notification item."""

    payload = item.model_dump(by_alias=True, mode="json")
    matched = match_policy_detail(item)
    payload["detectedAppName"] = _detected_app_name(item, matched)
    payload["appPolicy"] = matched.policy.to_prompt_dict()
    payload["appPolicyMatch"] = matched.to_prompt_dict()
    return payload


def match_policy_detail(item: NotificationItem) -> PolicyMatch:
    """정책과 매치 근거를 함께 찾는다. `appName` → `title` → `text` 순서다."""

    dictionary = load_app_dictionary()
    fields = {
        "appName": item.app_name,
        "title": item.title,
        "text": item.text,
    }

    for field_name in _MATCH_FIELDS:
        haystack = (fields.get(field_name) or "").casefold()
        if not haystack:
            continue
        # 같은 필드 안에서는 더 길고 구체적인 alias 가 이긴다. 목록 순서에 기대지 않는다.
        best: tuple[int, NotificationAppPolicy, str] | None = None
        for policy in dictionary.policies:
            for alias in policy.aliases:
                if alias.casefold() in haystack and (
                    best is None or len(alias) > best[0]
                ):
                    best = (len(alias), policy, alias)
        if best is not None:
            _, policy, alias = best
            return PolicyMatch(policy=policy, field=field_name, alias=alias)

    return PolicyMatch(policy=dictionary.fallback_policy)


def match_policy(item: NotificationItem) -> NotificationAppPolicy:
    """매치된 정책만 필요할 때 쓰는 얇은 래퍼."""

    return match_policy_detail(item).policy


def _detected_app_name(item: NotificationItem, matched: PolicyMatch) -> str:
    if matched.is_fallback or not matched.alias:
        return item.app_name
    return matched.alias
