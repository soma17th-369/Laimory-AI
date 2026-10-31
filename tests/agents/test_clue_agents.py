"""v4 사진·알림 단서 Agent 검증 (#169).

두 Agent 모두 입력 항목을 하나도 버리지 않는다. 모델이 응답에서 빠뜨리거나 호출이 실패해도
사진은 해석 없는 단서로, 알림은 내용 없는 단서로 남는다.
"""

import json

from app.agents.events.notification.clue_agent import (
    NotificationClueAgent,
    build_notification_clue_payload,
)
from app.agents.events.photo.clue_agent import PhotoClueAgent
from app.agents.events.photo.describer import PhotoDescriber
from app.schemas.clues import NotificationClueKind
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import make_request, notification_item, photo_item


class _KeepDescriptions(PhotoDescriber):
    def describe(self, photos):
        return {}


def _photo_agent(responses):
    llm = FakeLLM(responses)
    return PhotoClueAgent(llm=llm, describer=_KeepDescriptions()), llm


# --- 사진 --------------------------------------------------------------------------


def test_photo_clues_carry_the_interpretation_by_short_id():
    late = photo_item("late", taken="2026-06-20T19:00:00", lat=37.5, lon=127.0, places=["OO식당"])
    early = photo_item("early", taken="2026-06-20T09:00:00")
    agent, llm = _photo_agent(
        [json.dumps({"photos": [{"id": "P2", "activity": "순두부찌개를 먹었다", "uncertainty": ["동행"]}]})]
    )

    result = agent.generate(make_request(photos=[late, early]))

    first, second = result.clues
    assert (first.clue_id, first.raw_id) == ("P1", early.raw_id)
    assert (second.clue_id, second.raw_id, second.activity) == ("P2", late.raw_id, "순두부찌개를 먹었다")
    assert first.activity is None, "응답에서 빠진 사진은 해석 없이 남는다."
    assert (second.latitude, second.longitude) == (37.5, 127.0)


def test_photo_prompt_carries_no_raw_id_coordinates_or_url():
    photo = photo_item("p", lat=37.5, lon=127.0, photo_url="https://example.com/p.jpg")
    agent, llm = _photo_agent([json.dumps({"photos": []})])

    agent.generate(make_request(photos=[photo]))

    prompt = llm.calls[0].prompt
    assert photo.raw_id not in prompt
    assert "latitude" not in prompt and "37.5" not in prompt
    assert "example.com" not in prompt
    assert '"id": "P1"' in prompt


def test_photo_interpretation_failure_keeps_every_photo():
    photo = photo_item("p")
    agent, _ = _photo_agent([RuntimeError("down")])

    result = agent.generate(make_request(photos=[photo]))

    [clue] = result.clues
    assert clue.raw_id == photo.raw_id and clue.activity is None
    assert result.warnings


def test_photo_outside_the_window_is_not_a_clue():
    outside = photo_item("old", taken="2026-06-19T12:00:00")
    agent, _ = _photo_agent([json.dumps({"photos": []})])

    assert agent.generate(make_request(photos=[outside])).clues == []


# --- 알림 --------------------------------------------------------------------------


def _notifications():
    chat_1 = notification_item("chat-1", "카카오톡", "김민수", posted="2026-06-20T10:00:00")
    chat_2 = notification_item("chat-2", "카카오톡", "김민수", posted="2026-06-20T10:05:00")
    pay = notification_item("pay", "토스", "결제 완료", posted="2026-06-20T12:30:00")
    other = notification_item("etc", "자리톡", "안내", posted="2026-06-20T15:00:00")
    return chat_1, chat_2, pay, other


def test_payload_uses_short_ids_and_groups_conversations():
    items = _notifications()

    payload = build_notification_clue_payload(list(items))

    text = json.dumps(payload.body, ensure_ascii=False)
    assert all(item.raw_id not in text for item in items)
    [conversation] = payload.body["conversations"]
    assert conversation["id"] == "g1"
    assert [m.raw_id for m in payload.members["g1"]] == [items[0].raw_id, items[1].raw_id]
    assert {entry["id"] for entry in payload.body["notifications"] + payload.body["unclassified"]} == {"n1", "n2"}


def test_every_notification_lands_in_exactly_one_clue():
    chat_1, chat_2, pay, other = _notifications()
    payload = build_notification_clue_payload([chat_1, chat_2, pay, other])
    pay_id = next(e["id"] for e in payload.body["notifications"])
    response = {
        "clues": [
            {"kind": "CONVERSATION", "items": ["g1"], "counterpart": "김민수", "summary": "약속 시간을 물었다"},
            {"kind": "PAYMENT", "items": [pay_id, pay_id], "counterpart": "OO식당", "summary": "점심값 결제"},
        ]
    }
    agent = NotificationClueAgent(llm=FakeLLM([json.dumps(response, ensure_ascii=False)]))

    result = agent.generate(make_request(notifications=[chat_1, chat_2, pay, other]))

    raw_ids = [raw_id for clue in result.clues for raw_id in clue.raw_ids]
    assert sorted(raw_ids) == sorted(item.raw_id for item in [chat_1, chat_2, pay, other])
    conversation = next(c for c in result.clues if c.kind is NotificationClueKind.CONVERSATION)
    assert (conversation.first_at.minute, conversation.last_at.minute) == (0, 5)
    unread = next(c for c in result.clues if other.raw_id in c.raw_ids)
    assert unread.kind is None and unread.summary is None
    assert [clue.clue_id for clue in result.clues] == ["N1", "N2", "N3"]


def test_sensitive_values_left_in_a_summary_are_dropped():
    _, _, pay, _ = _notifications()
    response = {"clues": [{"kind": "PAYMENT", "items": ["n1"], "summary": "카드 1234-5678-9012-3456 결제"}]}
    agent = NotificationClueAgent(llm=FakeLLM([json.dumps(response, ensure_ascii=False)]))

    [clue] = agent.generate(make_request(notifications=[pay])).clues

    assert clue.summary is None
    assert clue.kind is NotificationClueKind.PAYMENT


def test_notification_failure_keeps_every_notification_without_content():
    items = list(_notifications())
    agent = NotificationClueAgent(llm=FakeLLM([RuntimeError("down")]))

    result = agent.generate(make_request(notifications=items))

    assert sorted(r for c in result.clues for r in c.raw_ids) == sorted(i.raw_id for i in items)
    assert all(clue.summary is None for clue in result.clues)
    assert result.warnings
