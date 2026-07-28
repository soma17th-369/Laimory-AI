import json

from app.agents.events.notification.agent import (
    NotificationEventAgent,
    _notification_items_to_text,
)
from app.agents.events.notification.app_dictionary import (
    enrich_notification_item,
    load_app_dictionary,
)
from app.schemas import NotificationItem
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import fixture_raw_id, make_request


def test_notification_app_dictionary_is_loaded_from_json() -> None:
    dictionary = load_app_dictionary()

    categories = {policy.category for policy in dictionary.policies}
    assert any(policy.key == "kakao_talk" for policy in dictionary.policies)
    assert {"MESSENGER", "PAYMENT", "SCHEDULE"}.issubset(categories)
    assert dictionary.fallback_policy.key == "unknown"


def test_notification_app_policy_uses_text_when_app_name_is_in_text() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("notification-1"),
        postedAt="2026-06-20T09:00:00",
        appName="알 수 없음",
        title="새 메시지",
        text="카카오톡",
    )

    payload = enrich_notification_item(item)

    assert payload["detectedAppName"] == "카카오톡"
    assert payload["appPolicy"]["key"] == "kakao_talk"
    assert payload["appPolicy"]["category"] == "MESSENGER"
    assert "누구와 어떤 주제로 대화했는지" in payload["appPolicy"]["timelineUse"]
    assert "사용자가 보낸 말이 아니라 받은 메시지" in payload["appPolicy"]["userSpeechRule"]
    assert "대화방 이름" in payload["appPolicy"]["titleMeaning"]
    assert "그 사람이 보낸 메시지" in payload["appPolicy"]["contentMeaning"]


def test_notification_agent_prompt_contains_app_dictionary_and_policy() -> None:
    response = json.dumps({"candidates": [], "fragments": []}, ensure_ascii=False)
    llm = FakeLLM([response])
    request = make_request(
        notifications=[
            NotificationItem(
                rawId=fixture_raw_id("notification-1"),
                postedAt="2026-06-20T09:00:00",
                appName="알 수 없음",
                title="새 메시지",
                text="카카오톡",
            )
        ]
    )

    NotificationEventAgent(llm=llm).generate(request)

    prompt_payload_text = llm.calls[0].prompt
    assert '"appDictionary"' in prompt_payload_text
    assert '"detectedAppName": "카카오톡"' in prompt_payload_text
    assert '"key": "kakao_talk"' in prompt_payload_text
    assert '"userSpeechRule"' in prompt_payload_text
    assert '"titleMeaning"' in prompt_payload_text
    assert '"contentMeaning"' in prompt_payload_text


def test_notification_items_to_text_has_unknown_fallback_policy() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("notification-2"),
        postedAt="2026-06-20T10:00:00",
        appName="처음보는앱",
        title="알림",
        text="내용",
    )

    payload = json.loads(_notification_items_to_text([item]))

    assert payload["notifications"][0]["detectedAppName"] == "처음보는앱"
    assert payload["notifications"][0]["appPolicy"]["key"] == "unknown"
    assert payload["notifications"][0]["appPolicy"]["category"] == "UNKNOWN"


def test_single_kakao_message_is_sparse_fragment_signal() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("kakao-single-1"),
        postedAt="2026-06-20T11:00:00",
        appName="카카오톡",
        title="김민수",
        text="ㅋㅋㅋㅋ",
    )

    payload = json.loads(_notification_items_to_text([item]))

    analysis = payload["messengerAnalysis"]["conversations"][0]
    assert analysis["speaker"] == "김민수"
    assert analysis["count"] == 1
    assert analysis["hasDirectMeetingMention"] is False
    assert "fragment 또는 무시" in analysis["interpretation"]
    assert payload["notifications"][0]["messengerInterpretation"]["speakerField"] == "title"
    assert payload["notifications"][0]["messengerInterpretation"]["contentField"] == "text"


def test_repeated_kakao_messages_are_conversation_flow_not_schedule() -> None:
    items = [
        NotificationItem(
            rawId=fixture_raw_id(f"kakao-repeat-{index}"),
            postedAt=f"2026-06-20T11:0{index}:00",
            appName="카카오톡",
            title="김민수",
            text=text,
        )
        for index, text in enumerate(
            ["오늘 코드 봤어", "그 부분 고민됨", "나중에 다시 이야기하자"],
            start=1,
        )
    ]

    payload = json.loads(_notification_items_to_text(items))

    analysis = payload["messengerAnalysis"]["conversations"][0]
    assert analysis["speaker"] == "김민수"
    assert analysis["count"] == 3
    assert analysis["hasDirectMeetingMention"] is False
    assert "가까운 사람과의 대화 흐름" in analysis["interpretation"]


def test_kakao_direct_meeting_mention_is_appointment_signal() -> None:
    items = [
        NotificationItem(
            rawId=fixture_raw_id("kakao-meeting-1"),
            postedAt="2026-06-20T18:00:00",
            appName="카카오톡",
            title="박지현",
            text="오늘 7시에 어디서 볼래?",
        )
    ]

    payload = json.loads(_notification_items_to_text(items))

    analysis = payload["messengerAnalysis"]["conversations"][0]
    assert analysis["hasDirectMeetingMention"] is True
    assert "약속 단서" in analysis["interpretation"]
    assert (
        payload["notifications"][0]["messengerInterpretation"]["hasDirectMeetingMention"]
        is True
    )


def test_kakao_topic_without_meeting_does_not_allow_place_label() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("kakao-ssafy-topic-1"),
        postedAt="2026-06-20T13:00:00",
        appName="카카오톡",
        title="김민수",
        text="SSAFY 채용설명회 글 봤어?",
    )

    payload = json.loads(_notification_items_to_text([item]))
    notification = payload["notifications"][0]

    assert notification["timelineUseGuidance"]["contextOnly"] is True
    assert notification["timelineUseGuidance"]["requiresOtherSourcesForSchedule"] is True
    assert notification["timelineUseGuidance"]["placeLabelAllowedFromNotification"] is False
    assert notification["messengerInterpretation"]["hasDirectMeetingMention"] is False


def test_kakao_direct_place_meeting_allows_place_label_signal() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("kakao-place-meeting-1"),
        postedAt="2026-06-20T18:00:00",
        appName="카카오톡",
        title="박지현",
        text="이따 수성못에서 보자",
    )

    payload = json.loads(_notification_items_to_text([item]))
    notification = payload["notifications"][0]

    assert notification["timelineUseGuidance"]["placeLabelAllowedFromNotification"] is True
    assert notification["timelineUseGuidance"]["explicitPlaceMention"] == "이따 수성못"
    assert notification["messengerInterpretation"]["placeLabelAllowed"] is True
    assert notification["messengerInterpretation"]["explicitPlaceMention"] == "이따 수성못"


def test_youtube_news_promotion_is_low_priority_fragment_only() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("youtube-1"),
        postedAt="2026-06-20T21:00:00",
        appName="YouTube",
        title="추천 영상",
        text="새로운 뉴스 요약 영상이 올라왔습니다",
    )

    payload = json.loads(_notification_items_to_text([item]))
    notification = payload["notifications"][0]

    assert notification["appPolicy"]["key"] == "media_info"
    assert notification["appPolicy"]["category"] == "LOW_PRIORITY"
    assert notification["timelineUseGuidance"]["shouldNotCreateTimelineEvent"] is True
    assert notification["timelineUseGuidance"]["lowConfidenceFragmentOnly"] is True


def test_unknown_notification_requires_other_sources_for_schedule() -> None:
    item = NotificationItem(
        rawId=fixture_raw_id("unknown-1"),
        postedAt="2026-06-20T22:00:00",
        appName="알수없는앱",
        title="알림",
        text="새로운 정보가 있습니다",
    )

    payload = json.loads(_notification_items_to_text([item]))
    notification = payload["notifications"][0]

    assert notification["appPolicy"]["key"] == "unknown"
    assert notification["timelineUseGuidance"]["requiresOtherSourcesForSchedule"] is True
    assert notification["timelineUseGuidance"]["shouldNotCreateTimelineEvent"] is True
