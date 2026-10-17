"""Notification 결과의 민감정보·관계명 검사 (#56 §5.4).

알림 원문은 마스킹되지 않은 채 Agent 에 들어간다. `NotificationItem` 이 수집이 준
`title`/`text` 를 그대로 갖고 있어서 JWT·계좌번호가 섞여 있을 수 있고, 한 번 새면
사용자가 읽는 일기에 그대로 남는다.

관계명도 같다. 알림 `title` 은 보낸 사람이나 대화방 이름이지 관계가 아니다.
`엄마` 같은 호칭은 User Memory 에 등록돼 있거나 입력 이름 그대로일 때만 쓸 수 있다.
호칭은 어절로 찾는다 — `이모티콘` 은 `이모` 가 아니다(#163).
"""

import pytest

from app.schemas import AgentEventResult, TimelineDraft
from app.services.notification_guard import (
    verify_notification_draft,
    verify_notification_result,
)
from tests.fixtures.fake_llm import candidate, fragment
from tests.fixtures.requests import fixture_raw_id, make_request, notification_item

NOTI_1 = fixture_raw_id("noti-1")


def _result_with_title(title: str) -> AgentEventResult:
    item = candidate("SOCIAL", [("NOTIFICATION", "noti-1")])
    item["title"] = title
    return AgentEventResult.model_validate({"candidates": [item], "fragments": []})


def _messages(result: AgentEventResult) -> str:
    return " ".join(warning.message for warning in result.warnings)


@pytest.mark.parametrize(
    "leaked",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w",
        "Bearer sk-abcdefghijklmnopqrstuvwx",
        "카드 1234-5678-9012-3456 승인",
        "010-1234-5678 로 연락",
    ],
)
def test_sensitive_values_are_detected(leaked: str) -> None:
    result = _result_with_title(leaked)

    verify_notification_result(result, make_request())

    assert "민감정보로 보이는 값" in _messages(result)


def test_clean_text_passes():
    result = _result_with_title("팀과 일정 조율")

    verify_notification_result(result, make_request())

    assert result.warnings == []


def test_relation_term_without_user_memory_is_flagged():
    result = _result_with_title("엄마와 통화")

    verify_notification_result(result, make_request())

    assert "관계 호칭" in _messages(result)


@pytest.mark.parametrize(
    "title",
    [
        # #163: 글자가 들어 있다고 호칭이 아니다. 잘못 잡으면 Repair 가 멀쩡한 문장을 고친다.
        "이모티콘 반응",
        "이모지만 보낸 답장",
        "형태가 바뀐 계획",
        "대형 마트에서 장보기",
        "원형 테이블 회의",
        "형제갈비에서 점심",
        "성형외과 진료",
        "전형선과 대화",
        "울동생과 대화",
    ],
)
def test_a_word_that_merely_contains_a_relation_term_is_not_flagged(title: str) -> None:
    result = _result_with_title(title)

    verify_notification_result(result, make_request())

    assert "관계 호칭" not in _messages(result)


@pytest.mark.parametrize("title", ["엄마와 통화", "동생이랑 저녁", "누나가 보낸 사진", "팀장님께 보고"])
def test_a_relation_term_with_a_particle_is_flagged(title: str) -> None:
    result = _result_with_title(title)

    verify_notification_result(result, make_request())

    assert "관계 호칭" in _messages(result)


def test_relation_term_that_is_the_input_room_name_is_allowed():
    # 대화방 이름이 `엄마` 면 `엄마와 대화` 는 입력 이름을 그대로 쓴 것이다.
    request = make_request(notifications=[notification_item(1, "카카오톡", "엄마")])
    result = _result_with_title("엄마와 안부 대화")

    verify_notification_result(result, request)

    assert "관계 호칭" not in _messages(result)


def test_an_input_name_that_merely_contains_a_term_does_not_allow_it():
    # `전형선` 이라는 이름이 있다고 `형` 을 써도 되는 것은 아니다.
    request = make_request(notifications=[notification_item(1, "카카오톡", "전형선")])
    result = _result_with_title("형과 저녁")

    verify_notification_result(result, request)

    assert "관계 호칭" in _messages(result)


def test_relation_term_registered_in_user_memory_is_allowed():
    request = make_request(
        user_memory={"relationships": "엄마(김영희)와 매주 통화합니다."}
    )
    result = _result_with_title("엄마와 통화")

    verify_notification_result(result, request)

    assert "관계 호칭" not in _messages(result)


def test_fragment_summary_is_also_checked():
    result = AgentEventResult.model_validate(
        {
            "candidates": [],
            "fragments": [
                fragment("NOTIFICATION", NOTI_1, "계좌 123-456-78901 안내 수신")
            ],
        }
    )

    verify_notification_result(result, make_request())

    assert "민감정보로 보이는 값" in _messages(result)


def test_empty_result_is_a_no_op():
    result = AgentEventResult()

    verify_notification_result(result, make_request())

    assert result.warnings == []


def _draft(title: str) -> TimelineDraft:
    return TimelineDraft.model_validate(
        {
            "userId": "user-1234",
            "date": "2026-06-20",
            "timezone": "Asia/Seoul",
            "events": [
                {
                    "clientEventId": "event-001",
                    "eventType": "SOCIAL",
                    "title": title,
                    "description": "알림을 바탕으로 정리한 사건",
                    "startTime": "2026-06-20T12:00:00+09:00",
                    "endTime": "2026-06-20T12:30:00+09:00",
                    "confidence": 0.7,
                    "inferenceLevel": "EVIDENCE_BASED",
                    "sourceRefs": [
                        {"sourceType": "NOTIFICATION", "rawId": NOTI_1}
                    ],
                    "uncertainty": [],
                }
            ],
            "questions": [],
            "warnings": [],
        }
    )


def test_sensitive_value_in_final_event_is_high_severity_warning():
    draft = _draft("카드 1234-5678-9012-3456 승인")

    verify_notification_draft(draft, make_request())

    assert len(draft.warnings) == 1
    assert draft.warnings[0].severity.value == "HIGH"
    assert "최종 event에 민감정보" in draft.warnings[0].message


def test_unsupported_relation_in_final_event_is_warned():
    draft = _draft("엄마와 통화")

    verify_notification_draft(draft, make_request())

    assert len(draft.warnings) == 1
    assert "최종 event에 User Memory 근거가 없는 관계 호칭" in draft.warnings[0].message


def test_final_draft_checks_relation_terms_only_in_what_the_user_reads():
    # 장소·근거 설명은 입력 이름이거나 사용자에게 나가지 않는 값이다.
    draft = _draft("식당에서 점심")
    event = draft.events[0]
    event.place = "이모네 식당"
    event.uncertainty = ["엄마와의 관계는 알 수 없다"]

    verify_notification_draft(draft, make_request())

    assert draft.warnings == []


def test_final_draft_warning_is_measured_again_on_every_confirm():
    # Repair 가 고친 뒤에도 앞 확정의 warning 이 남으면 이미 고친 문장을 또 고치려 든다.
    draft = _draft("엄마와 통화")
    verify_notification_draft(draft, make_request())
    assert len(draft.warnings) == 1

    draft.events[0].title = "가족과 통화"
    verify_notification_draft(draft, make_request())

    assert draft.warnings == []


def test_final_draft_does_not_pile_up_the_same_warning():
    draft = _draft("엄마와 통화")

    verify_notification_draft(draft, make_request())
    verify_notification_draft(draft, make_request())

    assert len(draft.warnings) == 1
