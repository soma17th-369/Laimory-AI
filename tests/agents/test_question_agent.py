"""Question Agent 파싱·적용 규칙 테스트 (이슈 #66).

여기서 지키는 것은 **코드가 확정하는 것들**이다. 질문이 좋은 질문인지는 프롬프트의
몫이라 여기서 재지 않는다. 대신 모델이 규칙을 어겼을 때 그 질문이 결과로 새지
않는다는 것과, 모든 event 가 질문을 갖도록 한 번 더 묻는다는 것을 본다.
"""

import json

import pytest

import app.agents.question.question_agent as question_module
from app.agents.parsing import user_memory_to_text
from app.agents.question import QuestionAgent
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    UserMemory,
)
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item


def _event(
    client_event_id: str = "event-001",
    *,
    event_type: EventType = EventType.MEAL,
    title: str = "점심을 먹었어요",
) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title,
        description="회사 근처에서 점심을 해결했어요.",
        start_time="2026-06-20T12:00:00+09:00",
        end_time="2026-06-20T13:00:00+09:00",
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=fixture_raw_id("s-1"))
        ],
    )


def _draft(*events: TimelineEventDraft) -> TimelineDraft:
    return TimelineDraft(
        user_id="user-1234",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=list(events),
    )


def _text(payload: object) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def _agent(*payloads: object) -> QuestionAgent:
    """응답을 순서대로 돌려주는 Agent. 두 번째부터는 재요청 응답이다."""

    return QuestionAgent(llm=FakeLLM([_text(p) for p in payloads]))


def _questions(*pairs: tuple[str, str]) -> dict:
    return {
        "questions": [
            {"clientEventId": event_id, "question": question}
            for event_id, question in pairs
        ]
    }


def _run(agent: QuestionAgent, draft: TimelineDraft) -> TimelineDraft:
    return agent.generate(make_request(), draft)


# --- 기본 적용 -------------------------------------------------------------


def test_attaches_each_question_to_its_event():
    draft = _draft(_event("event-001"), _event("event-002", title="회의에 참석했어요"))
    agent = _agent(
        _questions(
            ("event-001", "점심은 어떤 자리였나요?"),
            ("event-002", "회의에서 어떤 이야기가 기억에 남았나요?"),
        )
    )

    result = _run(agent, draft)

    assert result.events[0].question == "점심은 어떤 자리였나요?"
    assert result.events[1].question == "회의에서 어떤 이야기가 기억에 남았나요?"
    assert result.warnings == []


def test_every_event_type_gets_a_question():
    """수면·기상·이동도 예외가 아니다. 남길 말이 있는지는 사용자가 판단한다."""

    draft = _draft(
        _event("event-001", event_type=EventType.SLEEP, title="잠들었어요"),
        _event("event-002", event_type=EventType.WAKE_UP, title="일어났어요"),
        _event("event-003", event_type=EventType.MOVEMENT, title="이동했어요"),
    )
    agent = _agent(
        _questions(
            ("event-001", "잠들기 전에 어떤 생각이 남아 있었나요?"),
            ("event-002", "눈을 떴을 때 몸 상태는 어땠나요?"),
            ("event-003", "이동하는 동안 무엇을 하며 보냈나요?"),
        )
    )

    result = _run(agent, draft)

    assert all(event.question for event in result.events)
    assert result.warnings == []


def test_no_events_means_no_llm_call():
    llm = FakeLLM(['{"questions": []}'])

    result = QuestionAgent(llm=llm).generate(make_request(), _draft())

    assert llm.calls == []
    assert result.events == []


# --- 재요청 ---------------------------------------------------------------


def test_asks_again_for_the_events_left_out():
    draft = _draft(_event("event-001"), _event("event-002"))
    agent = _agent(
        _questions(("event-001", "점심은 어떤 자리였나요?")),
        _questions(("event-002", "그 시간은 어떻게 보내셨나요?")),
    )

    result = _run(agent, draft)

    assert result.events[0].question == "점심은 어떤 자리였나요?"
    assert result.events[1].question == "그 시간은 어떻게 보내셨나요?"
    assert result.warnings == []


def test_retry_asks_only_about_the_missing_events():
    llm = FakeLLM(
        [
            _text(_questions(("event-001", "점심은 어떤 자리였나요?"))),
            _text(_questions(("event-002", "그 시간은 어떻게 보내셨나요?"))),
        ]
    )
    draft = _draft(_event("event-001"), _event("event-002"))

    QuestionAgent(llm=llm).generate(make_request(), draft)

    assert len(llm.calls) == 2
    retry_prompt = llm.calls[1].prompt
    assert "event-002" in retry_prompt
    assert "event-001" not in retry_prompt


def test_retry_failure_keeps_what_the_first_pass_produced():
    draft = _draft(_event("event-001"), _event("event-002"))
    agent = QuestionAgent(
        llm=FakeLLM(
            [
                _text(_questions(("event-001", "점심은 어떤 자리였나요?"))),
                RuntimeError("의도된 실패"),
            ]
        )
    )

    result = _run(agent, draft)

    assert result.events[0].question == "점심은 어떤 자리였나요?"
    assert result.events[1].question is None
    assert any("기록 질문" in w.message for w in result.warnings)


def test_events_still_unanswered_after_retry_get_a_warning():
    draft = _draft(_event("event-001"), _event("event-002"))
    # 두 응답 모두 event-001 만 답한다.
    agent = _agent(
        _questions(("event-001", "점심은 어떤 자리였나요?")),
        _questions(("event-001", "점심은 어떤 자리였나요?")),
    )

    result = _run(agent, draft)

    assert result.events[1].question is None
    [warning] = result.warnings
    assert "1개" in warning.message


# --- 응답 검증 -------------------------------------------------------------


def test_drops_question_for_unknown_event():
    draft = _draft(_event("event-001"))
    agent = _agent(_questions(("event-999", "그때 어땠나요?")))

    result = _run(agent, draft)

    assert result.events[0].question is None


def test_keeps_only_the_first_question_per_event():
    draft = _draft(_event("event-001"))
    agent = _agent(
        _questions(
            ("event-001", "무엇이 기억에 남았나요?"),
            ("event-001", "기분은 어땠나요?"),
        )
    )

    result = _run(agent, draft)

    assert result.events[0].question == "무엇이 기억에 남았나요?"


def test_drops_statement_that_is_not_a_question():
    draft = _draft(_event("event-001"))
    agent = _agent(_questions(("event-001", "점심을 드셨습니다.")))

    result = _run(agent, draft)

    assert result.events[0].question is None


def test_drops_question_longer_than_the_contract_limit():
    draft = _draft(_event("event-001"))
    agent = _agent(_questions(("event-001", "그" * 255 + "?")))

    result = _run(agent, draft)

    # 자르지 않고 버린다. 잘린 질문은 문장이 끝나지 않아 물음이 되지 못한다.
    assert result.events[0].question is None


def test_keeps_valid_questions_when_one_item_is_malformed():
    draft = _draft(_event("event-001"), _event("event-002"))
    agent = _agent(
        {
            "questions": [
                {"question": "clientEventId 가 없어요?"},
                {"clientEventId": "event-002", "question": "점심은 어떤 자리였나요?"},
            ]
        }
    )

    result = _run(agent, draft)

    assert result.events[1].question == "점심은 어떤 자리였나요?"


def test_raises_when_the_first_response_has_no_json():
    draft = _draft(_event("event-001"))

    with pytest.raises(Exception):
        _run(_agent("죄송하지만 질문을 만들 수 없습니다."), draft)


# --- 프롬프트 계약 ----------------------------------------------------------


def test_prompt_hides_system_judgement_fields():
    """confidence·근거 같은 시스템 정보는 프롬프트에 실리지 않는다."""

    llm = FakeLLM([_text(_questions(("event-001", "점심은 어떤 자리였나요?")))])
    draft = _draft(_event("event-001"))

    QuestionAgent(llm=llm).generate(make_request(), draft)

    prompt = llm.calls[0].prompt
    assert "confidence" not in prompt
    assert "sourceRefs" not in prompt
    assert "inferenceLevel" not in prompt
    assert fixture_raw_id("s-1") not in prompt
    # 분 단위 시각도 주지 않는다 — 주지 않으면 질문에 새지 않는다.
    assert "12:00" not in prompt


def test_prompt_states_the_expected_question_count():
    llm = FakeLLM([_text(_questions(("event-001", "점심은 어떤 자리였나요?")))])
    draft = _draft(_event("event-001"), _event("event-002"))

    QuestionAgent(llm=llm).generate(make_request(), draft)

    assert "2개" in llm.calls[0].prompt


# --- v4: 장소 (#182) --------------------------------------------------------


@pytest.fixture
def v4(monkeypatch):
    monkeypatch.setattr(question_module, "uses_clue_contract", lambda: True)


@pytest.fixture
def v3(monkeypatch):
    monkeypatch.setattr(question_module, "uses_clue_contract", lambda: False)


def _place_event(client_event_id: str = "event-001", **overrides) -> TimelineEventDraft:
    event = _event(client_event_id, title="서대문구 대현동에서 마포구 도화동으로 출근")
    fields = {
        "description": "{place}에서 제육볶음을 먹었다.",
        "place": "정든식당",
        "place_candidates": ["정든식당", "포스트타워마포"],
        "address": "서울 마포구 도화동 33",
        **overrides,
    }
    return event.model_copy(update=fields)


def test_v4_prompt_carries_no_place(v4):
    llm = FakeLLM([_text(_questions(("event-001", "출근길에 무슨 생각을 하셨나요?")))])
    request = make_request(stays=[stay_item("s", address="서울 서대문구 대현동 11")])

    QuestionAgent(llm=llm).generate(request, _draft(_place_event()))

    prompt = llm.calls[0].prompt
    assert '"place"' not in prompt
    assert "{place}" not in prompt
    for word in ("정든식당", "대현동", "도화동", "마포구", "서대문구"):
        assert word not in prompt
    assert '"title": "출근"' in prompt
    assert '"description": "제육볶음을 먹었다."' in prompt


def test_v4_keeps_the_event_when_only_a_place_was_written(v4):
    llm = FakeLLM([_text(_questions(("event-001", "오늘 식사에서 제일 맛있었던 건 뭐예요?")))])
    event = _place_event(title="정든식당", description="{place}")

    QuestionAgent(llm=llm).generate(make_request(), _draft(event))

    prompt = llm.calls[0].prompt
    assert '"title": ""' in prompt
    assert '"description"' not in prompt
    assert '"eventType": "MEAL"' in prompt


def test_v4_leaves_the_text_users_read_alone(v4):
    event = _place_event()
    before = event.model_dump()
    agent = _agent(_questions(("event-001", "출근길에 무슨 생각을 하셨나요?")))

    result = _run(agent, _draft(event))

    assert result.events[0].model_dump() == {**before, "question": "출근길에 무슨 생각을 하셨나요?"}


def test_v4_user_memory_reaches_the_prompt_as_the_same_string(v4):
    """Timeline 과 같은 문자열을 본다. User Memory 속 장소는 프롬프트가 막는다."""

    memory = UserMemory(life_context="회사는 강남구 역삼동 센터필드에 있음.")
    llm = FakeLLM([_text(_questions(("event-001", "오늘 식사에서 제일 맛있었던 건 뭐예요?")))])

    QuestionAgent(llm=llm).generate(make_request(user_memory=memory), _draft(_place_event()))

    assert user_memory_to_text(memory) in llm.calls[0].prompt


def test_v3_prompt_still_carries_the_place_and_keeps_such_questions(v3):
    event = _event("event-001", title="정든식당에서 점심").model_copy(update={"place": "정든식당"})
    llm = FakeLLM([_text(_questions(("event-001", "정든식당 음식은 어땠어요?")))])

    result = QuestionAgent(llm=llm).generate(make_request(), _draft(event))

    prompt = llm.calls[0].prompt
    assert '"title": "정든식당에서 점심"' in prompt
    assert '"place": "정든식당"' in prompt
    assert len(llm.calls) == 1
    assert result.events[0].question == "정든식당 음식은 어땠어요?"
