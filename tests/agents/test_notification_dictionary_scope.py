"""app dictionary 매칭 정확도와 주입 범위 (#67).

정책 하나가 잘못 붙으면 `shouldNotCreateTimelineEvent` 가 달려 알림이 조용히
사라진다. 그래서 매칭은 넓게가 아니라 정확하게 한다. 그리고 사전 전체를 매번 싣지
않는다 — 알림 두세 건짜리 하루에도 등장하지 않은 앱의 정책까지 토큰을 차지했다.
"""

import json

import pytest

from app.agents.events.notification.agent import _notification_items_to_text
from app.agents.events.notification.app_dictionary import (
    app_dictionary_for_prompt,
    load_app_dictionary,
    match_policy_detail,
)
from app.schemas import NotificationItem
from tests.fixtures.requests import fixture_raw_id


def _item(app_name: str, title: str = "알림", text: str = "내용") -> NotificationItem:
    return NotificationItem(
        rawId=fixture_raw_id(f"{app_name}-{title}-{text}"),
        postedAt="2026-06-20T09:00:00",
        appName=app_name,
        title=title,
        text=text,
    )


# --- 매칭 --------------------------------------------------------------------


def test_app_name_wins_over_body_text() -> None:
    """알림이 어느 앱에서 왔는지는 `appName` 에 적혀 있다."""

    matched = match_policy_detail(_item("카카오톡", title="김민수", text="뉴스 봤어?"))

    assert matched.policy.key == "kakao_talk"
    assert matched.field == "appName"


def test_body_text_is_a_fallback_when_app_name_is_unknown() -> None:
    """수집기가 실제 앱 이름을 본문에 넣는 경우가 있어 남겨 둔 경로다."""

    matched = match_policy_detail(_item("알 수 없음", title="새 메시지", text="카카오톡"))

    assert matched.policy.key == "kakao_talk"
    assert matched.field == "text"


@pytest.mark.parametrize(
    "generic", ["사용", "취소", "매출", "추천", "혜택", "광고", "공지", "설문", "교육", "일정"]
)
def test_generic_words_in_a_body_no_longer_pull_a_policy(generic: str) -> None:
    """`사용`·`추천` 같은 일반 단어는 어느 앱 알림에나 나온다.

    예전에는 이런 단어가 alias 여서, 사전에 없는 앱의 본문 한 단어가 정책을 끌어왔고
    그 정책이 `LOW_PRIORITY`·`CONTEXT` 면 알림이 조용히 억제됐다.

    `예약`·`결제`·`승인` 은 일부러 남겼다. 그 단어 자체가 사건의 의미를 담고 있고,
    해당 정책은 `SCHEDULE`·`PAYMENT` 라 event 를 억제하는 쪽이 아니라 허용하는
    쪽으로 작동한다. 잘못 붙어도 알림이 사라지지 않는다.
    """

    matched = match_policy_detail(_item("처음보는앱", title="알림", text=generic))

    assert matched.is_fallback, f"'{generic}' 가 여전히 정책을 끌어옵니다."
    assert matched.policy.key == "unknown"


@pytest.mark.parametrize("kept", ["예약", "결제", "승인"])
def test_meaningful_words_are_kept_as_aliases(kept: str) -> None:
    """억제가 아니라 허용 쪽으로 작동하는 alias 는 남긴다."""

    matched = match_policy_detail(_item("처음보는앱", title="알림", text=kept))

    assert not matched.is_fallback
    assert matched.policy.category in {"SCHEDULE", "PAYMENT"}


def test_a_longer_alias_wins_within_the_same_field() -> None:
    """목록 순서가 유일한 우선순위이면 사전이 커질수록 나빠진다."""

    matched = match_policy_detail(_item("카카오페이"))

    assert matched.policy.key == "kakao_pay"
    assert matched.alias == "카카오페이"


def test_the_match_basis_is_recorded_for_diagnosis() -> None:
    payload = json.loads(_notification_items_to_text([_item("YouTube")]))
    notification = payload["notifications"][0]

    assert notification["appPolicyMatch"]["policyKey"] == "media_info"
    assert notification["appPolicyMatch"]["matchedField"] == "appName"
    # 억제 판정의 출처를 되짚을 수 있어야 한다.
    assert notification["timelineUseGuidance"]["suppressionBasis"] == (
        notification["appPolicyMatch"]
    )


# --- 주입 범위 ---------------------------------------------------------------


def test_only_matched_policies_are_injected() -> None:
    payload = json.loads(_notification_items_to_text([_item("카카오톡", title="김민수")]))

    keys = {policy["key"] for policy in payload["appDictionary"]}

    assert keys == {"kakao_talk", "unknown"}


def test_the_fallback_policy_is_always_included() -> None:
    """`UNKNOWN` 의 의미를 설명하는 정책이라 빠지면 안 된다."""

    payload = json.loads(_notification_items_to_text([_item("Toss")]))

    assert "unknown" in {policy["key"] for policy in payload["appDictionary"]}


def test_scoped_injection_is_smaller_than_the_whole_dictionary() -> None:
    payload = json.loads(_notification_items_to_text([_item("카카오톡", title="김민수")]))

    scoped = json.dumps(payload["appDictionary"], ensure_ascii=False)
    whole = json.dumps(app_dictionary_for_prompt(), ensure_ascii=False)

    assert len(scoped) < len(whole) / 2


def test_policy_wording_is_untouched() -> None:
    """이번 변경 대상은 매칭 규칙과 주입 범위다. 정책 문구는 유지한다."""

    kakao = next(
        policy
        for policy in load_app_dictionary().policies
        if policy.key == "kakao_talk"
    )

    assert "사용자가 보낸 말이 아니라 받은 메시지" in kakao.user_speech_rule
    assert "누구와 어떤 주제로 대화했는지" in kakao.timeline_use
