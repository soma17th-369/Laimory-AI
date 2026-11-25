"""Repair v4 의 입력과 도구 (#169·#171·#202).

v4 Repair 는 candidate 대신 코드가 정한 하루의 event 목록과 Timeline 의 이유를 읽는다. 하루의 event 와
그 시간·위치 기록·사진·일정 근거는 코드가 정해 Repair 는 글만 다듬는다(#202) — 나누기·흡수·시간 재맞춤·
재실행 도구가 없고, `update_event` 의 근거는 알림·대화뿐이다.
"""

import json
from datetime import datetime, timedelta, timezone

from app.agents.repair.repair_agent import build_repair_prompt
from app.agents.repair.tools import (
    RepairContext,
    available_tools,
    clue_evidence_text,
    execute_tool_calls,
)
from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    RepairToolCall,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import build_timeline_clues
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.requests import calendar_item, make_request, notification_item, photo_item, stay_item

_KST = timezone(timedelta(hours=9))


def _context():
    office = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00", places=["OO타워"])
    photo = photo_item("lunch", taken="2026-06-20T12:30:00")
    request = make_request(stays=[office], photos=[photo])
    clue = PhotoClue(
        clue_id="P1", raw_id=photo.raw_id, taken_at=datetime(2026, 6, 20, 12, 30, tzinfo=_KST),
        activity="순두부찌개를 먹었다",
    )
    clues = build_timeline_clues(build_location_skeleton(request), [], [clue], [])
    event = TimelineEventDraft(
        client_event_id="event-001",
        event_type=EventType.WORK,
        title="업무",
        description="{place}에서 일했다.",
        start_time=datetime(2026, 6, 20, 9, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 18, 0, tzinfo=_KST),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id)],
        merge_reason="회사 체류 하나",
        base_event_ids=["E1"],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[event])
    return RepairContext(request=request, draft=draft, extended=True, clues=clues), photo


def test_a_dropped_notification_is_not_in_the_repair_prompt():
    """Notification 이 버린 알림은 `근거 원본` 에도 없다(#173). 남긴 알림은 있다."""

    ctx, _ = _context()
    kept = notification_item("kept", "토스", "결제 완료", posted="2026-06-20T12:30:00")
    dropped = notification_item("dropped", "YouTube", "좋아요 알림", posted="2026-06-20T13:00:00")
    ctx.request.notifications.extend([kept, dropped])
    ctx.clues.notifications.append(
        NotificationClue(id="N1", kind=NotificationClueKind.PAYMENT, rawIds=[kept.raw_id], summary="결제")
    )

    prompt = build_repair_prompt(ctx, remaining=3)

    assert kept.raw_id in prompt
    assert dropped.raw_id not in prompt


def test_v4_prompt_carries_the_days_events_and_the_reasons():
    ctx, _ = _context()

    prompt = build_repair_prompt(ctx, remaining=3)
    evidence = json.loads(clue_evidence_text(ctx))

    assert "[event 근거]" in prompt
    assert [entry["id"] for entry in evidence["events"]] == ["E1"]
    assert evidence["draftEvents"] == [{"clientEventId": "event-001", "event": "E1", "reason": "회사 체류 하나"}]
    assert evidence["unusedClues"] == [], "쓰이지 않은 단서는 알림·대화뿐이다. 사진은 코드가 정한 방문의 것이다."

def test_v4_tools_only_write():
    """하루의 event 는 코드가 정한다(#202). 나누기·흡수·시간 재맞춤·재실행 도구가 없다."""

    ctx, _ = _context()

    tools = available_tools(ctx)

    for name in (
        "rerun_event_agent", "rerun_timeline_agent", "merge_stay_events", "split_event", "absorb_location_event",
        "repair_durations", "align_location_events", "enforce_meal_duration", "enforce_sleep_boundary",
        "ensure_calendar_events", "resolve_overlaps",
    ):
        assert name not in tools, name
    assert {"update_event", "delete_event", "add_conversation_event", "resolve_places", "lookup_source"} <= set(tools)
    assert "startTime" not in tools["update_event"].description

def test_repair_is_not_refused_for_what_it_writes():
    """코드는 거절할 권리가 없다(사용자 결정 2026-11-02). 9시간 체류를 근거 없이 식사로 써도 받고, 규칙 위반은 찾아서 넘긴다."""

    ctx, _ = _context()
    fields = {"eventType": "MEAL", "title": "OO타워에서 점심을 먹음", "description": "{place}에서 점심을 먹었다."}

    [result] = execute_tool_calls(
        ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": fields})]
    )

    event = ctx.draft.events[0]
    assert result.ok, result.message
    assert (event.event_type, event.title) == (EventType.MEAL, "OO타워에서 점심을 먹음")


def test_v4_repair_cannot_delete_an_event_of_the_day_but_can_delete_a_conversation():
    """코드가 정한 하루의 event 는 지우는 호출이 없다. 대화 event 는 지운다."""

    ctx, item, _ = _talking_context()
    assert _add(ctx).ok

    [refused, deleted] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(tool="delete_event", args={"clientEventId": "event-001"}),
            RepairToolCall(tool="delete_event", args={"clientEventId": ctx.draft.events[-1].client_event_id}),
        ],
    )

    assert not refused.ok and "update_event" in refused.message
    assert deleted.ok, deleted.message
    assert [event.client_event_id for event in ctx.draft.events] == ["event-001"]

def test_unused_clues_list_only_location_bundles_and_clues():
    """대화·사진의 자기 묶음 id 는 근거에 단서 id 로 잡혀, 쓴 뒤에도 쓰이지 않은 것으로 보였다(#183)."""

    ctx, item, _ = _talking_context()
    ctx.draft.events[0].source_refs.append(
        SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=item.raw_id)
    )

    unused = json.loads(clue_evidence_text(ctx))["unusedClues"]

    assert unused == ["N2"]


def _talking_context():
    """체류 event 하나와 쓰이지 않은 대화 둘."""

    office = stay_item("office", start="2026-06-20T09:00:00", end="2026-06-20T18:00:00", places=["OO타워"])
    first = notification_item("a", "카카오톡", "민수", posted="2026-06-20T12:00:00")
    second = notification_item("b", "카카오톡", "지수", posted="2026-06-20T13:00:00")
    request = make_request(stays=[office], notifications=[first, second])
    clues = build_timeline_clues(
        build_location_skeleton(request),
        [],
        [],
        [
            NotificationClue(
                id=f"N{number}", kind=NotificationClueKind.CONVERSATION, rawIds=[item.raw_id], summary="주말 계획",
                firstAt=datetime(2026, 6, 20, 11 + number, 0, tzinfo=_KST),
                lastAt=datetime(2026, 6, 20, 11 + number, 10, tzinfo=_KST),
            )
            for number, item in enumerate([first, second], start=1)
        ],
    )
    frame = TimelineEventDraft(
        client_event_id="event-001",
        event_type=EventType.STAY,
        title="보낸 시간",
        description="{place}에서 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 9, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 18, 0, tzinfo=_KST),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=office.raw_id)],
        base_event_ids=["E1"],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[frame])
    return RepairContext(request=request, draft=draft, extended=True, clues=clues), first, second


def _add(ctx, conversation="N1"):
    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="add_conversation_event",
                args={"conversation": conversation, "title": "민수와 대화", "description": "민수에게서 주말 계획 메시지를 받았다."},
                reason="구체적인 주제가 있다",
            )
        ],
    )
    return result


def test_add_conversation_event_takes_time_and_evidence_from_the_clue():
    ctx, item, _ = _talking_context()

    result = _add(ctx)

    assert result.ok, result.message
    event = ctx.draft.events[-1]
    assert event.event_type is EventType.SOCIAL
    assert [ref.raw_id for ref in event.source_refs] == [item.raw_id]
    assert (event.start_time, event.end_time) == (
        datetime(2026, 6, 20, 12, 0, tzinfo=_KST),
        datetime(2026, 6, 20, 12, 10, tzinfo=_KST),
    )


def test_add_conversation_event_rejects_what_is_not_a_conversation():
    ctx, _, _ = _talking_context()
    ctx.clues.notifications[0].kind = NotificationClueKind.PAYMENT

    assert not _add(ctx).ok
    assert not _add(ctx, "N9").ok
    assert len(ctx.draft.events) == 1


def test_add_conversation_event_rejects_a_conversation_already_used():
    ctx, item, _ = _talking_context()
    ctx.draft.events[0].source_refs.append(
        SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=item.raw_id)
    )

    assert not _add(ctx).ok


def test_add_conversation_event_rejects_a_fourth_conversation():
    ctx, _, _ = _talking_context()
    for number in range(3):
        extra = notification_item(f"x{number}", "카카오톡", "친구", posted=f"2026-06-20T1{number}:00:00")
        ctx.request.notifications.append(extra)
        ctx.clues.notifications.append(
            NotificationClue(id=f"N{number + 3}", kind=NotificationClueKind.CONVERSATION, rawIds=[extra.raw_id])
        )
        ctx.draft.events.append(
            ctx.draft.events[0].model_copy(
                update={
                    "client_event_id": f"event-t{number}",
                    "event_type": EventType.SOCIAL,
                    "source_refs": [SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=extra.raw_id)],
                    "base_event_ids": [],
                }
            )
        )

    result = _add(ctx)

    assert not result.ok
    assert "3개" in result.message


def test_add_conversation_event_is_not_held_back_by_the_event_limit():
    """대화는 전체 10개와 상관없이 들어간다(사용자 결정 2026-11-02)."""

    ctx, _, _ = _talking_context()
    ctx.draft.events.extend(
        ctx.draft.events[0].model_copy(update={"client_event_id": f"event-f{number}"}) for number in range(9)
    )

    result = _add(ctx)

    assert result.ok, result.message
    assert len(ctx.draft.events) == 11


def test_add_conversation_event_is_a_v4_tool():
    ctx, _, _ = _talking_context()
    assert "add_conversation_event" in available_tools(ctx)

    legacy = RepairContext(request=ctx.request, draft=ctx.draft, extended=True)
    assert "add_conversation_event" not in available_tools(legacy)


def test_update_event_takes_notifications_and_leaves_the_codes_evidence():
    ctx, photo = _context()
    pay = notification_item("pay", "토스", "결제", posted="2026-06-20T12:31:00")
    ctx.request.notifications.append(pay)
    ctx.clues.notifications.append(NotificationClue(id="N1", kind=NotificationClueKind.PAYMENT, rawIds=[pay.raw_id]))

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"evidence": ["N1", "P1", "E1"]}},
                reason="점심 결제를 근거로 더한다",
            )
        ],
    )

    assert result.ok, result.message
    raw_ids = [ref.raw_id for ref in ctx.draft.events[0].source_refs]
    assert raw_ids == [ctx.request.stays[0].raw_id, pay.raw_id]
    assert "P1, E1" in result.message, "코드가 정한 근거라 바꾸지 않았다고 알린다."

def test_update_event_with_unknown_evidence_runs_and_says_so():
    """코드는 Repair 의 호출을 거절하지 않는다. 펼칠 것이 없는 id 는 바꾸지 않았다고 결과에 적는다."""

    ctx, _ = _context()

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"title": "일함", "evidence": ["B9"]}},
                reason="없는 묶음",
            )
        ],
    )

    assert result.ok and "B9" in result.message
    assert ctx.draft.events[0].title == "일함"

def test_update_event_does_not_move_a_photo():
    """사진은 코드가 정한 방문의 것이다(#202). Repair 가 사진 id 를 적어도 다른 event 로 옮기지 않는다."""

    ctx, photo = _context()
    talk = TimelineEventDraft(
        client_event_id="event-002", event_type=EventType.SOCIAL, title="대화", description="메시지를 받았다.",
        start_time=datetime(2026, 6, 20, 12, 30, tzinfo=_KST), end_time=datetime(2026, 6, 20, 12, 30, tzinfo=_KST),
        confidence=0.7, inference_level=InferenceLevel.DIRECT,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=ctx.request.stays[0].raw_id)],
    )
    ctx.draft.events.append(talk)

    [result] = execute_tool_calls(
        ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-002", "fields": {"evidence": ["P1"]}})]
    )

    assert result.ok and "P1" in result.message
    assert photo.raw_id not in {ref.raw_id for ref in talk.source_refs}

def test_update_event_does_not_change_the_time():
    ctx, _ = _context()

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"endTime": "2026-06-20T12:00:00+09:00", "title": "일함"}},
            )
        ],
    )

    assert result.ok and "endTime" in result.message
    assert ctx.draft.events[0].end_time == datetime(2026, 6, 20, 18, 0, tzinfo=_KST)
    assert ctx.draft.events[0].title == "일함"

    [only_time] = execute_tool_calls(
        ctx,
        [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": {"startTime": "2026-06-20T10:00:00+09:00"}})],
    )
    assert only_time.ok and "바뀐 것이 없습니다" in only_time.message, "시간만 보내도 오류가 아니라 바꾸지 않았다고 알린다."


def test_update_event_does_not_take_source_refs_or_places():
    """근거와 장소도 코드가 정한 값이다(#203). `sourceRefs` 로 다른 방문의 사진을 붙이면 다음 확정까지
    사진이 두 event 에 있었고, 도구는 고쳤다고만 답했다. 호출은 실행하고 글은 적용한다."""

    ctx, photo = _context()
    ctx.draft.events[0].source_refs.append(SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id))
    talk = TimelineEventDraft(
        client_event_id="event-002", event_type=EventType.SOCIAL, title="대화", description="메시지를 받았다.",
        start_time=datetime(2026, 6, 20, 12, 30, tzinfo=_KST), end_time=datetime(2026, 6, 20, 12, 30, tzinfo=_KST),
        confidence=0.7, inference_level=InferenceLevel.DIRECT,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=ctx.request.stays[0].raw_id)],
    )
    ctx.draft.events.append(talk)
    before = list(talk.source_refs)
    moved = [SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id).model_dump(by_alias=True, mode="json")]

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={
                    "clientEventId": "event-002",
                    "fields": {"sourceRefs": moved, "place": "OO식당", "address": "어딘가", "title": "점심 대화"},
                },
            )
        ],
    )

    assert result.ok, result.message
    assert "sourceRefs" in result.message and "place" in result.message and "evidence" in result.message
    talk = next(e for e in ctx.draft.events if e.client_event_id == "event-002")  # update_event 는 새 객체로 바꿔 끼운다
    assert talk.title == "점심 대화", "글은 적용한다."
    assert talk.source_refs == before and talk.place is None and talk.address is None
    holders = [e.client_event_id for e in ctx.draft.events if photo.raw_id in {r.raw_id for r in e.source_refs}]
    assert holders == ["event-001"], "사진은 코드가 정한 방문 하나에만 있다."

def test_a_long_stay_with_its_meal_photo_can_say_so():
    """긴 체류에 음식 사진이 있으면 `점심을 먹고 시간을 보냈다` 로 쓸 수 있다(사용자 결정). 길이는 타입에만 본다."""

    ctx, _ = _context()

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={
                    "clientEventId": "event-001",
                    "fields": {"description": "{place}에서 점심을 먹고 시간을 보냈다.", "evidence": ["B1", "P1"]},
                },
                reason="문장",
            )
        ],
    )

    assert result.ok, result.message


def test_update_event_clears_the_writing_mark_only_when_it_writes():
    """코드가 맡긴 event 는 Repair 가 글을 쓰면 끝난다. 다른 값만 고쳐서는 끝나지 않는다."""

    from app.services.skeleton_guard import NEEDS_WRITING

    ctx, _ = _context()
    event = ctx.draft.events[0]
    event.merge_reason = NEEDS_WRITING

    def call(fields):
        [result] = execute_tool_calls(
            ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": fields}, reason="글")]
        )
        assert result.ok, result.message

    call({"confidence": 0.8})
    assert ctx.draft.events[0].merge_reason == NEEDS_WRITING

    call({"title": "서비스 배포 준비"})
    assert ctx.draft.events[0].merge_reason == NEEDS_WRITING, "제목만 고치면 문장이 빈 채로 남는다."

    call({"description": "{place}에서 서비스 배포를 준비했다."})
    assert ctx.draft.events[0].merge_reason is None


def test_repair_goes_on_while_writing_is_left():
    """마지막 확정이 맡긴 글이 있으면 `done` 이어도 한 번 더 돈다. 코드는 대신 쓰지 않는다."""

    from app.agents.repair.repair_agent import _writing_left
    from app.services.confirm_report import ConfirmReport

    ctx, _ = _context()
    assert not _writing_left(ctx)

    report = ConfirmReport()
    report.findings = [{"kind": "EVENT_TO_WRITE"}]
    ctx.reports.append(report)
    assert _writing_left(ctx)

    report.findings = [{"kind": "PHOTO_MISMATCH"}]
    assert _writing_left(ctx), "그 event 의 사진과 다른 사진으로 쓴 글도 다시 써야 한다(#202)."

    report.findings = [{"kind": "DURATION_OVER_LIMIT"}]
    assert not _writing_left(ctx)


def _write_v4(ctx, fields):
    [result] = execute_tool_calls(
        ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": fields})]
    )
    assert result.ok, result.message
    return result.message


def test_v4_update_event_records_the_photos_its_text_used():
    """글과 함께 적은 사진 id 를 남기고 코드가 그 event 의 사진과 맞춰 본다(#202). 다르면 결과에 알리되 고친 것은
    받는다 — 코드는 Repair 를 거절하지 않는다(사용자 결정 2026-11-02)."""

    ctx, _ = _context()
    event = lambda: ctx.draft.events[0]  # noqa: E731 — update_event 는 event 객체를 갈아 끼운다

    message = _write_v4(ctx, {"photos": ["P1"], "description": "{place}에서 순두부찌개를 먹었다."})
    assert event().photo_ids == ["P1"] and "사진" not in message
    assert json.loads(clue_evidence_text(ctx))["draftEvents"][0]["photos"] == ["P1"]

    message = _write_v4(ctx, {"photos": [{"id": "P7", "activity": "망고 디저트를 먹음"}], "description": "{place}에서 망고 디저트를 먹었다."})
    assert event().description == "{place}에서 망고 디저트를 먹었다.", "거절하지 않는다."
    assert "P7 는 이 event 의 사진이 아닙니다" in message and "사진 P1 를 글이 쓰지 않았습니다" in message

    message = _write_v4(ctx, {"description": "{place}에서 점심을 먹었다."})
    assert event().photo_ids == [], "사진을 적지 않고 다시 쓴 글은 어느 사진으로 썼는지 모른다."
    assert "사진 P1 를 글이 쓰지 않았습니다" in message

    message = _write_v4(ctx, {"photos": ["P1"]})
    assert event().photo_ids == ["P1"] and "글이 쓴 사진을 적었습니다" in message

    _write_v4(ctx, {"title": "순두부찌개를 먹음"})
    assert event().photo_ids == ["P1"], "문장을 건드리지 않은 호출은 적은 사진을 그대로 둔다."


def test_v4_prompt_shows_only_the_latest_confirm():
    """앞 회차의 보정 기록은 다시 싣지 않는다(#196). v3 는 지금처럼 쌓는다."""

    from app.agents.repair.tools import confirm_report_text
    from app.services.confirm_report import ConfirmReport

    ctx, _ = _context()
    for sequence, event_id in ((1, "event-001"), (2, "event-002")):
        report = ConfirmReport(sequence=sequence)
        report.corrected = [{"clientEventId": event_id, "step": "근거 정리", "changes": {}}]
        ctx.reports.append(report)

    shown = json.loads(confirm_report_text(ctx))
    assert [item["clientEventId"] for item in shown["corrected"]] == ["event-002"]

    ctx.clues = None
    stacked = json.loads(confirm_report_text(ctx))
    assert [item["clientEventId"] for item in stacked["corrected"]] == ["event-001", "event-002"]


def _calendar_context(*, title="팀 점심 식사", end_hour=13, end_minute=30):
    calendar = calendar_item("lunch", title, start="2026-06-20T12:00:00", end=f"2026-06-20T{end_hour}:{end_minute:02d}:00")
    request = make_request(calendars=[calendar])
    clues = build_timeline_clues(build_location_skeleton(request), build_calendar_clues(request), [], [])
    event = TimelineEventDraft(
        client_event_id="event-001",
        event_type=EventType.CALENDAR_EVENT,
        title=title,
        description=f"{title} 일정이 있었다.",
        start_time=datetime(2026, 6, 20, 12, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, end_hour, end_minute, tzinfo=_KST),
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=EventSourceType.CALENDAR, raw_id=calendar.raw_id)],
    )
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[event])
    return RepairContext(request=request, draft=draft, extended=True, clues=clues)


def _update(ctx, fields):
    [result] = execute_tool_calls(
        ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": fields}, reason="타입을 고른다")]
    )
    return result


def test_update_event_takes_a_calendar_as_the_evidence_of_its_meal():
    """일정 시간 그대로인 event 는 일정이 식사의 근거다(#184). 도구가 거절해 식사 일정을 분류할 수 없었다."""

    ctx = _calendar_context()

    result = _update(ctx, {"eventType": "MEAL"})

    assert result.ok, result.message
    assert ctx.draft.events[0].event_type is EventType.MEAL
