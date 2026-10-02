"""Repair 가 확정 pass 에서 받는 입력 (#119).

Repair 는 코드가 확정한 draft 를 받는다. 예전에는 코드가 무엇을 고쳤는지가 warning 문장으로만
전해져, Repair 가 코드가 이미 본 것까지 다시 검증했다. 여기서는 세 가지를 본다.

    1. 코드가 고친 것과 찾은 것이 구조화된 기록으로 프롬프트에 실린다.
    2. event 마다 Event Agent 가 해석한 근거(candidate·fragment)가 실린다.
    3. 돌 때마다 그 시점의 draft 로 다시 만든다.

새 입력은 v3 세트에서만 싣는다. 테스트는 세트와 무관하게 확인하려고 `extended_input` 을
직접 준다.
"""

import json

from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.agents.repair import RepairAgent
from app.agents.repair.repair_agent import build_repair_prompt
from app.agents.repair.tools import RepairContext, event_evidence_text
from app.core import langfuse_tracing
from app.schemas import (
    AgentEventResult,
    AiEventCandidate,
    CandidateTimeRange,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceFragment,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
    UserMemory,
)
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    movement_item,
    photo_item,
    stay_item,
)

DAY = "2026-06-20"

OUT = fixture_raw_id("move-out")
BACK = fixture_raw_id("move-back")
OFFICE = fixture_raw_id("stay-office")
PHOTO = fixture_raw_id("photo-1")


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _request(**overrides):
    defaults = dict(
        movements=[
            movement_item(1, raw_id="move-out", start=f"{DAY}T08:20:00", end=f"{DAY}T09:00:00"),
            movement_item(2, raw_id="move-back", start=f"{DAY}T18:00:00", end=f"{DAY}T19:00:00"),
        ],
        stays=[
            stay_item(
                1,
                raw_id="stay-office",
                start=f"{DAY}T09:00:00",
                end=f"{DAY}T18:00:00",
                place="회사",
                places=["회사"],
            )
        ],
    )
    defaults.update(overrides)
    return make_request(**defaults)


def _event(
    client_event_id: str,
    start: str,
    end: str,
    *raw_ids: str,
    event_type=EventType.MOVEMENT,
    title: str = "회사에 다녀옴",
) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title,
        description="회사에 다녀왔어요.",
        start_time=_t(start),
        end_time=_t(end),
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[
            SourceRef(source_type=EventSourceType.STAY, raw_id=raw_id)
            for raw_id in raw_ids
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )


def _plan(tool_calls: list[dict], *, done: bool = False) -> str:
    return json.dumps(
        {"issues": [], "toolCalls": tool_calls, "done": done, "summary": "요약"},
        ensure_ascii=False,
    )


_DONE = _plan([], done=True)

_SPLIT = _plan(
    [
        {
            "tool": "split_event",
            "args": {
                "clientEventId": "event-001",
                "parts": [
                    {"startTime": _t("08:20"), "endTime": _t("09:00"), "title": "회사로 출근"},
                    {
                        "startTime": _t("09:00"),
                        "endTime": _t("18:00"),
                        "title": "회사에서 근무",
                        "eventType": "WORK",
                    },
                    {"startTime": _t("18:00"), "endTime": _t("19:00"), "title": "집으로 귀가"},
                ],
            },
        }
    ]
)


def _section(prompt: str, name: str) -> str:
    """`[name]` 절의 본문. 다음 절이 시작되는 곳까지다."""

    body = prompt.split(f"[{name}]\n", 1)[1]
    return body.split("\n\n[", 1)[0]


def _report(prompt: str) -> dict:
    return json.loads(_section(prompt, "자동 검사 결과"))


def _journey() -> TimelineDraft:
    return _draft(_event("event-001", "08:20", "19:00", OUT, OFFICE, BACK))


# --- 자동 검사 결과 --------------------------------------------------------------


def test_prompt_carries_what_the_code_found():
    llm = FakeLLM([_DONE])

    RepairAgent(llm=llm, max_iterations=1, extended_input=True).generate(
        _request(), _journey()
    )

    (finding,) = _report(llm.calls[0].prompt)["findings"]
    assert finding["kind"] == "LONG_STAY_BETWEEN_MOVEMENTS"
    assert finding["clientEventId"] == "event-001"
    assert [stay["rawId"] for stay in finding["longStays"]] == [OFFICE]
    assert [
        (segment["kind"], segment["startAt"], segment["endAt"])
        for segment in finding["segments"]
    ] == [
        ("MOVEMENT", _t("08:20"), _t("09:00")),
        ("STAY", _t("09:00"), _t("18:00")),
        ("MOVEMENT", _t("18:00"), _t("19:00")),
    ]


def test_prompt_carries_what_the_code_corrected():
    """근거 밖으로 나간 시간을 코드가 잘랐다. 그 event 의 문장은 다시 봐야 한다."""

    llm = FakeLLM([_DONE])
    draft = _draft(
        _event("event-001", "08:00", "18:00", OFFICE, event_type=EventType.WORK, title="근무")
    )

    RepairAgent(llm=llm, max_iterations=1, extended_input=True).generate(_request(), draft)

    corrected = _report(llm.calls[0].prompt)["corrected"]
    aligned = [item for item in corrected if item["step"] == "근거 구간 정렬"]
    assert aligned[0]["confirm"] == 1
    assert aligned[0]["clientEventId"] == "event-001"
    assert aligned[0]["changes"]["startTime"] == {"before": _t("08:00"), "after": _t("09:00")}


def test_prompt_says_so_when_the_code_did_nothing():
    llm = FakeLLM([_DONE])
    draft = _draft(
        _event("event-001", "09:00", "11:00", OFFICE, event_type=EventType.WORK, title="근무")
    )
    draft.events[0].place = "회사"
    draft.events[0].address = "서울특별시 어딘가"

    RepairAgent(llm=llm, max_iterations=1, extended_input=True).generate(_request(), draft)

    assert _section(llm.calls[0].prompt, "자동 검사 결과") == (
        "코드가 고치거나 찾은 것이 없습니다."
    )


def test_findings_are_recomputed_on_every_iteration():
    """Repair 가 나눈 뒤에는 그 위반이 더 실리지 않는다. 실리면 또 고치려 든다."""

    llm = FakeLLM([_SPLIT, _DONE])

    result = RepairAgent(llm=llm, max_iterations=3, extended_input=True).generate(
        _request(), _journey()
    )

    first, second = (_report(call.prompt) for call in llm.calls)
    assert [item["kind"] for item in first["findings"]] == ["LONG_STAY_BETWEEN_MOVEMENTS"]
    # 나눈 체류 조각은 9시간짜리 근무다. 이동이라는 경계로 나눈 뒤라 찾은 것이 없고, 9시간은
    # `WORK` 의 검토 기준 아래라 길이도 짚지 않는다(#134).
    assert "findings" not in second
    assert [event.title for event in result.events] == [
        "회사로 출근",
        "회사에서 근무",
        "집으로 귀가",
    ]


def test_every_iteration_sees_the_draft_the_previous_one_left():
    llm = FakeLLM([_SPLIT, _DONE])

    RepairAgent(llm=llm, max_iterations=3, extended_input=True).generate(
        _request(), _journey()
    )

    first, second = (json.loads(_section(call.prompt, "draft")) for call in llm.calls)
    assert [event["title"] for event in first["events"]] == ["회사에 다녀옴"]
    assert [event["title"] for event in second["events"]] == [
        "회사로 출근",
        "회사에서 근무",
        "집으로 귀가",
    ]


def test_corrections_accumulate_across_iterations():
    """첫 확정에서 고친 것은 다음 차례에도 실린다. 다듬을 기회가 한 번뿐이면 안 된다."""

    rename = _plan(
        [
            {
                "tool": "update_event",
                "args": {"clientEventId": "event-001", "fields": {"title": "회사에서 근무"}},
            }
        ]
    )
    llm = FakeLLM([rename, _DONE])
    draft = _draft(
        _event("event-001", "08:00", "18:00", OFFICE, event_type=EventType.WORK, title="근무")
    )

    RepairAgent(llm=llm, max_iterations=3, extended_input=True).generate(_request(), draft)

    second = _report(llm.calls[1].prompt)["corrected"]
    assert [item["confirm"] for item in second if item["step"] == "근거 구간 정렬"] == [1]


def test_candidate_with_the_same_structure_is_not_a_finding():
    """찾은 것은 draft 의 event 에 대한 것만 싣는다.

    Repair 는 candidate 를 고칠 수 없어 그 지적이 끝까지 남는다. 실제 LLM 은 그것을
    해소하려고 위반이 아닌 event(20분 이하 체류를 낀 이동)까지 나눴다.
    """

    candidate = AiEventCandidate(
        event_type=EventType.MOVEMENT,
        time_range=CandidateTimeRange(start_time=_t("08:20"), end_time=_t("19:00")),
        title="회사 왕복",
        description="회사에 다녀왔다.",
        source_refs=[
            SourceRef(source_type=EventSourceType.MOVEMENT, raw_id=OUT),
            SourceRef(source_type=EventSourceType.STAY, raw_id=OFFICE),
            SourceRef(source_type=EventSourceType.MOVEMENT, raw_id=BACK),
        ],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
    )
    llm = FakeLLM([_DONE])

    RepairAgent(llm=llm, max_iterations=1, extended_input=True).generate(
        _request(),
        _journey(),
        event_results={"location": AgentEventResult(candidates=[candidate])},
    )

    findings = _report(llm.calls[0].prompt)["findings"]
    assert [item["kind"] for item in findings] == ["LONG_STAY_BETWEEN_MOVEMENTS"]
    assert [item["title"] for item in findings] == ["회사에 다녀옴"]  # draft 의 event


# --- event 근거 -----------------------------------------------------------------


def _candidate(title: str, description: str, *raw_ids: str, uncertainty=()) -> AiEventCandidate:
    return AiEventCandidate(
        event_type=EventType.PHOTO_MOMENT,
        time_range=CandidateTimeRange(start_time=_t("12:30"), end_time=_t("12:30")),
        title=title,
        description=description,
        source_refs=[
            SourceRef(source_type=EventSourceType.PHOTO, raw_id=raw_id)
            for raw_id in raw_ids
        ],
        confidence=0.8,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        uncertainty=list(uncertainty),
    )


def _evidence_context(*events, results: dict[str, AgentEventResult]) -> RepairContext:
    return RepairContext(
        request=_request(photos=[photo_item("1", taken=f"{DAY}T12:30:00")]),
        draft=_draft(*events),
        event_results=results,
        extended=True,
    )


def test_evidence_carries_what_the_event_agent_read():
    """raw 입력의 한 줄 요약에는 사진에 무엇이 찍혔는지가 없다."""

    ctx = _evidence_context(
        _event("event-001", "12:00", "13:00", OFFICE, PHOTO, event_type=EventType.MEAL),
        results={
            "photo": AgentEventResult(
                candidates=[
                    _candidate(
                        "김밥과 라면",
                        "김밥천국 간판 아래 김밥과 라면이 놓인 식탁.",
                        PHOTO,
                        uncertainty=["누구와 먹었는지는 알 수 없다."],
                    )
                ]
            )
        },
    )

    payload = json.loads(event_evidence_text(ctx))

    assert payload["events"] == [
        {"clientEventId": "event-001", "candidateIds": ["candidate-001"], "fragments": []}
    ]
    assert payload["candidates"]["candidate-001"] == {
        "agent": "photo",
        "eventType": "PHOTO_MOMENT",
        "title": "김밥과 라면",
        "startTime": _t("12:30"),
        "endTime": _t("12:30"),
        "description": "김밥천국 간판 아래 김밥과 라면이 놓인 식탁.",
        "uncertainty": ["누구와 먹었는지는 알 수 없다."],
    }


def test_candidate_body_is_carried_once_however_many_events_use_it():
    ctx = _evidence_context(
        _event("event-001", "09:00", "12:00", OFFICE, event_type=EventType.WORK),
        _event("event-002", "13:00", "18:00", OFFICE, event_type=EventType.WORK),
        results={
            "location": AgentEventResult(
                candidates=[_candidate("회사 체류", "회사에 머물렀다.", OFFICE)]
            )
        },
    )

    payload = json.loads(event_evidence_text(ctx))

    assert list(payload["candidates"]) == ["candidate-001"]
    assert [entry["candidateIds"] for entry in payload["events"]] == [
        ["candidate-001"],
        ["candidate-001"],
    ]


def test_unused_candidate_is_listed_without_its_body():
    """Timeline 이 일부러 쓰지 않았을 수 있다. 본문까지 실으면 되살리려 든다."""

    ctx = _evidence_context(
        _event("event-001", "09:00", "12:00", OFFICE, event_type=EventType.WORK),
        results={
            "photo": AgentEventResult(
                candidates=[_candidate("쓰이지 않은 사진", "본문은 싣지 않는다.", PHOTO)]
            )
        },
    )

    payload = json.loads(event_evidence_text(ctx))

    assert payload["candidates"] == {}
    assert payload["unusedCandidates"] == [
        {
            "agent": "photo",
            "eventType": "PHOTO_MOMENT",
            "title": "쓰이지 않은 사진",
            "startTime": _t("12:30"),
            "endTime": _t("12:30"),
        }
    ]


def test_fragment_is_attached_to_the_event_that_cites_it():
    ctx = _evidence_context(
        _event("event-001", "12:00", "13:00", OFFICE, PHOTO, event_type=EventType.MEAL),
        results={
            "photo": AgentEventResult(
                fragments=[
                    SourceFragment(
                        source_type=EventSourceType.PHOTO,
                        raw_id=PHOTO,
                        summary="내용을 알 수 없는 사진",
                    )
                ]
            )
        },
    )

    payload = json.loads(event_evidence_text(ctx))

    assert payload["events"][0]["fragments"] == [
        {"agent": "photo", "summary": "내용을 알 수 없는 사진"}
    ]


def test_evidence_is_absent_when_no_agent_result_is_given():
    ctx = _evidence_context(
        _event("event-001", "09:00", "12:00", OFFICE, event_type=EventType.WORK),
        results={},
    )

    assert event_evidence_text(ctx) == "없음"


# --- 세트에 따른 입력 ------------------------------------------------------------


def test_extended_prompt_has_the_three_new_sections_before_the_sources():
    ctx = RepairContext(request=_request(), draft=_journey(), extended=True)

    prompt = build_repair_prompt(ctx, 3)

    assert (
        prompt.index("[draft]")
        < prompt.index("[자동 검사 결과]")
        < prompt.index("[event 근거]")
        < prompt.index("[user memory]")
        < prompt.index("[근거 원본]")
        < prompt.index("[사용 가능한 도구]")
    )


def test_plain_prompt_is_what_it_used_to_be():
    ctx = RepairContext(request=_request(), draft=_journey(), extended=False)

    prompt = build_repair_prompt(ctx, 3)

    assert [line for line in prompt.splitlines() if line.startswith("[")] == [
        "[draft]",
        "[근거 원본]",
        "[사용 가능한 도구]",
        "[지금까지 실행한 도구]",
        "[남은 반복 횟수] 이번 차례를 포함해 3번",
    ]


def test_split_event_is_offered_only_with_the_extended_input():
    """v2 프롬프트는 이 도구를 언제 쓰는지 모른다. 주면 일정대로인 event 까지 쪼갠다."""

    extended = RepairContext(request=_request(), draft=_journey(), extended=True)
    plain = RepairContext(request=_request(), draft=_journey(), extended=False)

    assert "split_event(" in _section(build_repair_prompt(extended, 3), "사용 가능한 도구")
    assert "split_event" not in build_repair_prompt(plain, 3)


def test_legacy_repair_sees_no_warning_it_cannot_act_on():
    """새 검사의 warning 은 draft 에 남아 프롬프트에 실린다. v2 에는 보이지 않아야 한다."""

    llm = FakeLLM([_DONE])

    result = RepairAgent(llm=llm, max_iterations=1, extended_input=False).generate(
        _request(), _journey()
    )

    assert "나눠야 합니다" not in llm.calls[0].prompt
    assert not [
        warning
        for warning in result.warnings
        if warning.warning_id.startswith("warning-movement-stay-")
    ]


def test_split_event_is_refused_without_the_extended_input():
    """카탈로그에 없는 도구는 불러도 실행하지 않는다."""

    llm = FakeLLM([_SPLIT, _DONE])

    result = RepairAgent(llm=llm, max_iterations=2, extended_input=False).generate(
        _request(), _journey()
    )

    assert [event.title for event in result.events] == ["회사에 다녀옴"]
    assert "없는 도구입니다" in llm.calls[1].prompt


# --- 관측 -----------------------------------------------------------------------


def test_trace_keeps_the_user_memory_body_out(monkeypatch) -> None:
    """이 관측은 프롬프트를 문자열로 통째로 싣는다. 키로 거르는 마스킹이 닿지 않는다."""

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-repair-memory",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:1",
        span_exporter=exporter,
    )
    monkeypatch.setattr(langfuse_tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(langfuse_tracing.settings, "langfuse_content_capture", "SANITIZED")
    memory = UserMemory.model_validate(
        {"schemaVersion": "1.0", "currentFocus": "결제 서비스 리뉴얼 프로젝트"}
    )
    llm = FakeLLM([_DONE])

    with langfuse_tracing.trace_observation("repair-agent", as_type="agent"):
        RepairAgent(llm=llm, max_iterations=1, extended_input=True).generate(
            _request(user_memory=memory), _journey()
        )
    client.flush()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    traced = spans["analyze-repair-iteration"].attributes["langfuse.observation.input"]
    assert "결제 서비스 리뉴얼 프로젝트" not in traced
    # 모델이 받은 프롬프트에는 본문이 그대로 있다.
    assert "결제 서비스 리뉴얼 프로젝트" in llm.calls[0].prompt
