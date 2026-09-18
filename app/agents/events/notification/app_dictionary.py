"""알림 앱 정책 사전 (#116).

원본은 Notion 「코드가 다루는 앱 목록」이다. 그 표에 있는 앱은 사용자가 알림을 누르지
않아도 수집되는 결제·예약 계열이고, 표에 없는 앱의 알림은 사용자가 직접 눌러 담은 것이다
(대부분 메신저). 정책이 없는 알림은 대화 묶음으로 간다.

메신저는 표 밖에서 정책 하나를 둔다(카카오톡·Instagram·Webex·Slack·Discord). 사용자가
고르는 앱이지만 가게·서비스가 알림톡으로 예약·결제·배송 안내를, 업무 메신저가 회의 안내를
보내므로 내용에 따라 대화·결제·예약 어느 것이든 될 수 있다(#116). 메신저 알림도 대화 묶음으로
가고, 묶음이 이 정책을 가리킨다.

정책은 그 앱에서 **어떤 정보를 얻을 수 있는지**만 말한다. candidate 로 만들지,
confidence 를 얼마로 둘지는 정하지 않는다 — 그건 알림 내용을 읽은 Agent 가 정한다.

매칭은 입력 ``appName`` 하나만 본다. 예전처럼 ``title``·``text`` 까지 뒤지면 표에 없는
앱의 알림도 본문에 `카드`·`사용`·`일정` 같은 단어 하나만 있으면 결제·일정 앱으로 잡힌다.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from app.schemas import CamelModel

_DICTIONARY_PATH = Path(__file__).with_name("app_dictionary.json")


class NotificationInfo(StrEnum):
    """알림에서 얻는 정보 종류. 대화·결제·예약 셋으로 고정한다(#116)."""

    CONVERSATION = "CONVERSATION"
    PAYMENT = "PAYMENT"
    RESERVATION = "RESERVATION"


class NotificationPolicy(CamelModel):
    """한 도메인의 앱에서 얻을 수 있는 정보."""

    policy_id: str = Field(alias="policyId", min_length=1)
    domain: str = Field(min_length=1)
    provides: list[NotificationInfo] = Field(min_length=1)
    information: str = Field(min_length=1)
    title_meaning: str = Field(alias="titleMeaning", min_length=1)
    text_meaning: str = Field(alias="textMeaning", min_length=1)

    def to_prompt_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class NotificationApp(CamelModel):
    """Notion 표의 한 행."""

    app_name: str = Field(alias="appName", min_length=1)
    #: 표 대조용이다. 수집기가 ``appName`` 에 패키지명을 싣는 경우에도 맞도록 매칭 키에 넣는다.
    application_id: str = Field(alias="applicationId", min_length=1)
    #: 입력 ``appName`` 이 표의 앱 이름과 다르게 오는 경우(`삼성 메시지` → `메시지`).
    aliases: list[str] = Field(default_factory=list)
    policy_ids: list[str] = Field(alias="policyIds", min_length=1)

    def match_keys(self) -> list[str]:
        return [self.app_name, self.application_id, *self.aliases]


class NotificationAppDictionary(CamelModel):
    policies: list[NotificationPolicy]
    apps: list[NotificationApp]

    @model_validator(mode="after")
    def _check_references(self) -> NotificationAppDictionary:
        policy_ids = [policy.policy_id for policy in self.policies]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("policyId 가 중복됩니다.")

        known = set(policy_ids)
        index: dict[str, tuple[str, ...]] = {}
        for app in self.apps:
            unknown = set(app.policy_ids) - known
            if unknown:
                raise ValueError(f"{app.app_name} 이 없는 정책을 가리킵니다: {sorted(unknown)}")
            for key in app.match_keys():
                normalized = normalize_app_name(key)
                previous = index.setdefault(normalized, tuple(app.policy_ids))
                # 같은 이름이 두 행에 걸리는 것은 정책이 같을 때만 허용한다.
                # `메시지` 는 구글·삼성 메시지 둘 다의 표시명이고 둘 다 문자다.
                if previous != tuple(app.policy_ids):
                    raise ValueError(f"'{key}' 가 서로 다른 정책의 앱 이름으로 겹칩니다.")
        return self

    def policy_ids_by_name(self) -> dict[str, tuple[str, ...]]:
        return {
            normalize_app_name(key): tuple(app.policy_ids)
            for app in self.apps
            for key in app.match_keys()
        }


def normalize_app_name(value: str) -> str:
    """공백과 대소문자만 무시한다. `카카오 T` 와 `카카오T` 는 같은 앱이다."""

    return "".join(value.split()).casefold()


@lru_cache(maxsize=1)
def load_app_dictionary() -> NotificationAppDictionary:
    """사전 파일을 프로세스당 한 번 읽는다."""

    raw = json.loads(_DICTIONARY_PATH.read_text(encoding="utf-8"))
    return NotificationAppDictionary.model_validate(raw)


@lru_cache(maxsize=1)
def _policy_ids_by_name() -> dict[str, tuple[str, ...]]:
    """앱 이름(정규화) → 정책 id. 알림을 정책에 잇는 데 쓴다."""

    return load_app_dictionary().policy_ids_by_name()


@lru_cache(maxsize=1)
def _policies_by_id() -> dict[str, NotificationPolicy]:
    """정책 id → 정책. 사전 순서를 유지한다."""

    return {policy.policy_id: policy for policy in load_app_dictionary().policies}


def match_policy_ids(app_name: str | None) -> tuple[str, ...]:
    """입력 ``appName`` 에 걸리는 정책 id. 표에 없는 앱이면 빈 tuple 이다."""

    if not app_name:
        return ()
    return _policy_ids_by_name().get(normalize_app_name(app_name), ())


def provides_conversation(policy_ids: tuple[str, ...]) -> bool:
    """정책 중 하나라도 대화를 주는가. 그런 앱의 알림은 대화 묶음으로 간다."""

    policies = _policies_by_id()
    return any(
        NotificationInfo.CONVERSATION in policies[policy_id].provides
        for policy_id in policy_ids
    )


def policies_for_prompt(policy_ids: list[str]) -> list[dict[str, Any]]:
    """주어진 정책만 사전 순서대로 돌려준다. 수신된 알림에 걸린 정책만 싣기 위한 것이다."""

    wanted = set(policy_ids)
    return [
        policy.to_prompt_dict()
        for policy_id, policy in _policies_by_id().items()
        if policy_id in wanted
    ]
