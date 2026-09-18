"""알림 앱 정책 사전과 Notification Agent 입력 payload (#116).

정책 원본은 Notion 「코드가 다루는 앱 목록」이다. 표에 있는 앱은 사용자가 알림을 누르지
않아도 수집되고(결제·예약 계열), 표에 없는 앱은 사용자가 직접 눌러 담은 알림이다(대부분
메신저). 메신저는 표 밖에서 개인·업무 두 정책을 갖는다. 코드는 정책과
대화 상대 묶음 같은 사실만 넘기고 판단은 모델에게 맡긴다.
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.events.notification.agent import (
    NotificationEventAgent,
    build_notification_payload,
)
from app.agents.events.notification.app_dictionary import (
    _DICTIONARY_PATH,
    NotificationAppDictionary,
    NotificationInfo,
    load_app_dictionary,
    match_policy_ids,
)
from app.schemas import NotificationItem
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import fixture_raw_id, make_request


def _item(
    label: str,
    app_name: str,
    title: str,
    text: str = "본문",
    posted: str = "2026-06-20T09:00:00+09:00",
) -> NotificationItem:
    return NotificationItem(
        rawId=fixture_raw_id(label),
        postedAt=posted,
        appName=app_name,
        title=title,
        text=text,
    )


# --- 사전 ------------------------------------------------------------------


_NOTION_POLICIES = [
    "FINANCE_PAYMENT",
    "SHOPPING_DELIVERY",
    "RESERVATION_CULTURE",
    "TRANSIT_TRAVEL",
    "FOOD_CAFE",
    "SMS",
]


def test_dictionary_mirrors_the_notion_app_list_plus_messengers() -> None:
    dictionary = load_app_dictionary()
    messenger_policies = {"MESSENGER", "WORK_MESSENGER"}
    notion_apps = [a for a in dictionary.apps if not messenger_policies & set(a.policy_ids)]
    by_policy = {
        policy: [a.app_name for a in dictionary.apps if a.policy_ids == [policy]]
        for policy in messenger_policies
    }

    assert [policy.policy_id for policy in dictionary.policies] == [
        *_NOTION_POLICIES,
        "MESSENGER",
        "WORK_MESSENGER",
    ]
    assert len(notion_apps) == 90
    assert by_policy["MESSENGER"] == ["카카오톡", "Instagram", "Discord"]
    assert by_policy["WORK_MESSENGER"] == ["Webex", "Slack", "Microsoft Teams"]
    assert len({app.app_name for app in dictionary.apps}) == 96
    assert len({app.application_id for app in dictionary.apps}) == 96


def test_notion_policies_provide_only_payment_or_reservation() -> None:
    """표의 앱은 결제·예약 계열이다. 대화는 사용자가 고른 알림에서 온다."""

    policies = {p.policy_id: p for p in load_app_dictionary().policies}
    provided = {info for pid in _NOTION_POLICIES for info in policies[pid].provides}

    assert provided == {NotificationInfo.PAYMENT, NotificationInfo.RESERVATION}


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [
        ("카카오톡", "MESSENGER"),
        ("com.kakao.talk", "MESSENGER"),
        ("Instagram", "MESSENGER"),
        ("인스타그램", "MESSENGER"),
        ("Discord", "MESSENGER"),
        ("Webex", "WORK_MESSENGER"),
        ("Slack", "WORK_MESSENGER"),
        ("Microsoft Teams", "WORK_MESSENGER"),
        ("Teams", "WORK_MESSENGER"),
    ],
)
def test_messenger_apps_match_their_policy(app_name: str, expected: str) -> None:
    assert match_policy_ids(app_name) == (expected,)


def test_personal_messenger_provides_all_three_kinds() -> None:
    """대화뿐 아니라 알림톡으로 온 예약·결제 안내도 개인 메신저로 온다."""

    policies = {p.policy_id: p for p in load_app_dictionary().policies}

    assert set(policies["MESSENGER"].provides) == set(NotificationInfo)


def test_work_messenger_provides_conversation_and_reservation() -> None:
    """업무 메신저는 업무 대화와 회의 안내가 오고 결제는 오지 않는다."""

    policies = {p.policy_id: p for p in load_app_dictionary().policies}

    assert set(policies["WORK_MESSENGER"].provides) == {
        NotificationInfo.CONVERSATION,
        NotificationInfo.RESERVATION,
    }


def test_naver_belongs_to_two_domains() -> None:
    assert match_policy_ids("네이버") == ("SHOPPING_DELIVERY", "RESERVATION_CULTURE")


def test_dictionary_rejects_a_name_shared_by_different_policies() -> None:
    raw = json.loads(_DICTIONARY_PATH.read_text(encoding="utf-8"))
    raw["apps"].append(
        {
            "appName": "가짜앱",
            "applicationId": "com.example.fake",
            "aliases": ["메시지"],
            "policyIds": ["FINANCE_PAYMENT"],
        }
    )

    with pytest.raises(ValidationError, match="메시지"):
        NotificationAppDictionary.model_validate(raw)


def test_dictionary_rejects_unknown_policy_reference() -> None:
    raw = {
        "policies": [],
        "apps": [
            {"appName": "앱", "applicationId": "com.example", "policyIds": ["NOPE"]}
        ],
    }

    with pytest.raises(ValidationError, match="NOPE"):
        NotificationAppDictionary.model_validate(raw)


# --- 매칭 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("app_name", "text"),
    [
        ("자리톡", "카드 사용 내역을 확인하세요"),
        ("YouTube", "멤버십 결제 혜택 영상"),
        ("처음보는앱", "오늘 일정 확인"),
    ],
)
def test_match_uses_app_name_only(app_name: str, text: str) -> None:
    """본문의 단어로 앱을 추정하지 않는다.

    #116 이전에는 title·text 까지 뒤져서, 표에 없는 앱의 알림도 `카드`·`사용`·`결제`·`일정`
    같은 단어 하나로 결제·일정 앱이 되고 "알림만으로 candidate 가능" 표시를 받았다.
    """

    item = _item(f"unlisted-{app_name}", app_name, "알림", text)

    payload = build_notification_payload([item])

    assert payload["notifications"] == []
    assert payload["policies"] == []
    assert payload["conversations"] == []
    assert payload["unclassified"][0]["rawId"] == item.raw_id


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [
        ("카카오T", ("TRANSIT_TRAVEL",)),  # 표는 `카카오 T`
        ("kb pay", ("FINANCE_PAYMENT",)),  # 표는 `KB Pay`
        ("메시지", ("SMS",)),  # 실제 수집 데이터의 표시명
        ("NOL", ("RESERVATION_CULTURE",)),  # 표는 `NOL(야놀자)`
        ("viva.republica.toss", ("FINANCE_PAYMENT",)),  # 패키지명으로 오는 수집기
    ],
)
def test_match_follows_the_values_that_arrive_as_app_name(
    app_name: str, expected: tuple[str, ...]
) -> None:
    assert match_policy_ids(app_name) == expected


@pytest.mark.parametrize("app_name", ["처음보는앱", "자리톡", "", None])
def test_unlisted_app_has_no_policy(app_name: str | None) -> None:
    assert match_policy_ids(app_name) == ()


# --- payload ---------------------------------------------------------------


def test_payload_carries_only_received_policies_once() -> None:
    items = [
        _item("toss-1", "토스", "결제", "스타벅스 5,000원 승인"),
        _item("toss-2", "토스", "결제", "GS25 2,000원 승인"),
        _item("eats-1", "쿠팡이츠", "주문", "배달 도착 예정"),
    ]

    payload = build_notification_payload(items)

    assert [policy["policyId"] for policy in payload["policies"]] == [
        "FINANCE_PAYMENT",
        "FOOD_CAFE",
    ]
    assert [n["policyIds"] for n in payload["notifications"]] == [
        ["FINANCE_PAYMENT"],
        ["FINANCE_PAYMENT"],
        ["FOOD_CAFE"],
    ]
    # 정책 본문은 알림마다 복사되지 않는다.
    assert "information" not in payload["notifications"][0]
    assert json.dumps(payload, ensure_ascii=False).count('"information"') == 2


def test_payload_has_no_code_made_judgment() -> None:
    items = [
        _item("kakao-1", "카카오톡", "김민수", "이따 수성못에서 보자"),
        _item("youtube-1", "YouTube", "채널", "새 영상"),
    ]

    text = json.dumps(build_notification_payload(items), ensure_ascii=False)

    for removed in (
        "appDictionary",
        "appPolicy",
        "detectedAppName",
        "timelineUseGuidance",
        "contextOnly",
        "messengerAnalysis",
        "messengerInterpretation",
        "interpretation",
    ):
        assert removed not in text, f"{removed} 가 payload 에 남아 있습니다."


def test_same_room_messages_become_one_conversation() -> None:
    items = [
        _item("room-2", "카카오톡", "17기 단톡", "두 번째", "2026-06-20T10:05:00+09:00"),
        _item("dm-1", "카카오톡", "김민수", "ㅋㅋ", "2026-06-20T09:00:00+09:00"),
        _item("room-1", "카카오톡", "17기 단톡", "첫 번째", "2026-06-20T10:00:00+09:00"),
        _item("room-3", "카카오톡", "17기 단톡 ", "세 번째", "2026-06-20T10:30:00+09:00"),
    ]

    room, dm = build_notification_payload(items)["conversations"]

    assert (room["title"], room["messageCount"]) == ("17기 단톡", 3)
    assert [m["text"] for m in room["messages"]] == ["첫 번째", "두 번째", "세 번째"]
    assert room["firstPostedAt"] == "2026-06-20T10:00:00+09:00"
    assert room["lastPostedAt"] == "2026-06-20T10:30:00+09:00"
    assert room["maxGapMinutes"] == 25.0
    assert (dm["title"], dm["messageCount"], dm["maxGapMinutes"]) == ("김민수", 1, None)


def test_conversations_with_equal_count_keep_earlier_first() -> None:
    items = [
        _item("late", "카카오톡", "나중 방", posted="2026-06-20T15:00:00+09:00"),
        _item("early", "카카오톡", "먼저 방", posted="2026-06-20T08:00:00+09:00"),
    ]

    titles = [c["title"] for c in build_notification_payload(items)["conversations"]]

    assert titles == ["먼저 방", "나중 방"]


def test_same_title_in_different_apps_is_not_one_conversation() -> None:
    items = [
        _item("kakao", "카카오톡", "김민수"),
        _item("slack", "Slack", "김민수"),
    ]

    conversations = build_notification_payload(items)["conversations"]

    assert sorted(c["appName"] for c in conversations) == ["Slack", "카카오톡"]


def test_messenger_goes_to_conversations_with_its_policy() -> None:
    """메신저는 정책이 있어도 대화 상대 단위로 묶이고, 묶음이 그 정책을 가리킨다."""

    items = [
        _item("kakao-1", "카카오톡", "캐치테이블", "[예약 확정] 오늘 19:00 2명"),
        _item("slack-1", "Slack", "박천웅", "회의록 올렸어요"),
    ]

    payload = build_notification_payload(items)

    assert payload["notifications"] == []
    assert [p["policyId"] for p in payload["policies"]] == ["MESSENGER", "WORK_MESSENGER"]
    by_title = {c["title"]: c for c in payload["conversations"]}
    assert by_title["캐치테이블"]["policyIds"] == ["MESSENGER"]
    assert by_title["박천웅"]["policyIds"] == ["WORK_MESSENGER"]


def test_unlisted_apps_are_listed_flat_not_grouped_as_conversations() -> None:
    """사전에 없는 앱을 대화로 묶으면 코드가 "대화다" 라고 먼저 정하는 셈이다."""

    items = [
        _item("etc-2", "자리톡", "안내", "우산 챙기세요", "2026-06-20T18:00:00+09:00"),
        _item("etc-1", "자리톡", "안내", "오늘 비 예보", "2026-06-20T07:00:00+09:00"),
        _item("yt-1", "YouTube", "채널", "새 영상", "2026-06-20T12:00:00+09:00"),
    ]

    payload = build_notification_payload(items)

    assert payload["conversations"] == []
    assert payload["policies"] == []
    assert [(n["appName"], n["text"]) for n in payload["unclassified"]] == [
        ("자리톡", "오늘 비 예보"),
        ("YouTube", "새 영상"),
        ("자리톡", "우산 챙기세요"),
    ]
    assert "policyIds" not in payload["unclassified"][0]


def test_payload_keeps_every_raw_id_exactly_once() -> None:
    items = [
        _item("pay", "토스", "결제"),
        _item("sms", "메시지", "1588-0000"),
        _item("chat-a", "카카오톡", "방"),
        _item("chat-b", "카카오톡", "방"),
        _item("other", "자리톡", "날씨"),
    ]

    payload = build_notification_payload(items)
    raw_ids = (
        [n["rawId"] for n in payload["notifications"]]
        + [m["rawId"] for c in payload["conversations"] for m in c["messages"]]
        + [n["rawId"] for n in payload["unclassified"]]
    )

    assert sorted(raw_ids) == sorted(item.raw_id for item in items)


def test_agent_sends_the_payload_to_the_llm() -> None:
    llm = FakeLLM([json.dumps({"candidates": [], "fragments": []})])
    request = make_request(notifications=[_item("etc", "자리톡", "안내")])

    NotificationEventAgent(llm=llm).generate(request)

    prompt = llm.calls[0].prompt
    assert '"unclassified"' in prompt
    assert '"policies": []' in prompt
