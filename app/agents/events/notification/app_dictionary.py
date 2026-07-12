"""Notification app dictionary loader and matcher."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field

from app.schemas import CamelModel, NotificationItem

_DICTIONARY_PATH = Path(__file__).with_name("app_dictionary.json")


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


@lru_cache(maxsize=1)
def load_app_dictionary() -> NotificationAppDictionary:
    """Load the editable app dictionary file once per process."""

    raw = json.loads(_DICTIONARY_PATH.read_text(encoding="utf-8"))
    return NotificationAppDictionary.model_validate(raw)


def app_dictionary_for_prompt() -> list[dict[str, Any]]:
    """Return app policies for the LLM prompt."""

    return [policy.to_prompt_dict() for policy in load_app_dictionary().policies]


def enrich_notification_item(item: NotificationItem) -> dict[str, Any]:
    """Attach matched app policy to a normalized notification item.

    Some collectors put the real app name in ``text`` rather than ``appName``.
    Matching therefore checks appName, title, and text together.
    """

    payload = item.model_dump(by_alias=True, mode="json")
    policy = match_policy(item)
    payload["detectedAppName"] = _detected_app_name(item, policy)
    payload["appPolicy"] = policy.to_prompt_dict()
    return payload


def match_policy(item: NotificationItem) -> NotificationAppPolicy:
    dictionary = load_app_dictionary()
    searchable = " ".join(
        value for value in (item.app_name, item.title, item.text) if value
    ).casefold()
    for policy in dictionary.policies:
        if any(alias.casefold() in searchable for alias in policy.aliases):
            return policy
    return dictionary.fallback_policy


def _detected_app_name(
    item: NotificationItem,
    policy: NotificationAppPolicy,
) -> str:
    if policy.key == load_app_dictionary().fallback_policy.key:
        return item.app_name

    values = (item.text, item.app_name, item.title)
    for alias in policy.aliases:
        alias_folded = alias.casefold()
        if any(alias_folded in value.casefold() for value in values if value):
            return alias
    return item.app_name
