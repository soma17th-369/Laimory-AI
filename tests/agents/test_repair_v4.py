"""Repair v4 의 입력과 도구 (#169·#171).

v4 Repair 는 candidate 대신 근거 묶음과 Timeline 의 병합 이유를 읽고, 도구 인자의 근거로
묶음·단서 id 를 쓴다. Event Agent 가 없어 `rerun_event_agent` 가 없고, 같은 장소 체류는 뼈대가
이미 묶어 `merge_stay_events` 도 없다.
"""

from datetime import datetime, timedelta, timezone

from app.agents.repair.repair_agent import build_repair_prompt
from app.agents.repair.tools import RepairContext, available_tools, execute_tool_calls
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
from app.services.clue_bundles import build_timeline_clues
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.requests import make_request, notification_item, photo_item, stay_item

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


def test_v4_prompt_carries_bundles_merge_reasons_and_unused_clues():
    ctx, _ = _context()

    prompt = build_repair_prompt(ctx, remaining=3)

    assert "[event 근거]" in prompt
    assert '"bundles"' in prompt and '"B1"' in prompt
    assert '"mergeReason": "회사 체류 하나"' in prompt
    assert '"unusedClues": [\n    "P1"\n  ]' in prompt or '"P1"' in prompt.split('"unusedClues"', 1)[1]


def test_v4_tools_have_no_event_agent_rerun_or_stay_merge():
    ctx, _ = _context()

    tools = available_tools(ctx)

    assert "rerun_event_agent" not in tools
    assert "merge_stay_events" not in tools
    assert "evidence" in tools["update_event"].description


def test_update_event_expands_clue_ids_into_source_refs():
    ctx, photo = _context()

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"evidence": ["B1", "P1"]}},
                reason="점심 사진을 근거로 더한다",
            )
        ],
    )

    assert result.ok, result.message
    raw_ids = [ref.raw_id for ref in ctx.draft.events[0].source_refs]
    assert photo.raw_id in raw_ids
    assert ctx.draft.events[0].source_refs[0].reason == "위치 구간"


def test_update_event_rejects_evidence_that_points_at_nothing():
    ctx, _ = _context()

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"evidence": ["B9"]}},
                reason="없는 묶음",
            )
        ],
    )

    assert not result.ok


def test_a_bundle_id_does_not_pull_in_a_photo_another_event_holds():
    """Repair 가 회의 event 의 근거를 `B5`·`C1` 로 다시 적자 B5 의 식사 사진까지 회의로 들어간 실제 출력.

    사진 단일 귀속은 일정이 있는 회의에 사진을 남겼고, 사진뿐이던 식사 event 가 지워졌다. 일정과 식사는
    다른 활동이라 둘 다 남아야 한다(사용자). 묶음을 펼칠 때 다른 event 가 가진 사진은 빼고, 단서 id 로
    직접 적은 사진만 담는다.
    """

    ctx, photo = _context()
    meal = TimelineEventDraft(
        client_event_id="event-002",
        event_type=EventType.MEAL,
        title="식사",
        description="{place}에서 순두부찌개를 먹었다.",
        start_time=datetime(2026, 6, 20, 12, 30, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 12, 50, tzinfo=_KST),
        confidence=0.8,
        inference_level=InferenceLevel.DIRECT,
        source_refs=[SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id)],
    )
    ctx.draft.events.append(meal)

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"evidence": ["B1"]}},
                reason="회의 근거만 남긴다",
            )
        ],
    )

    assert result.ok, result.message
    assert photo.raw_id not in [ref.raw_id for ref in ctx.draft.events[0].source_refs], "식사의 사진이다."

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-001", "fields": {"evidence": ["B1", "P1"]}},
                reason="사진을 직접 적으면 담는다",
            )
        ],
    )
    assert result.ok and photo.raw_id in [ref.raw_id for ref in ctx.draft.events[0].source_refs]
    # Repair 가 직접 고른 자리가 사진의 주인이다 — 코드는 Repair 를 따른다(사용자 결정 2026-10-30).
    assert photo.raw_id not in [ref.raw_id for ref in meal.source_refs], "사진은 한 event 에만 있다."


def test_update_event_rejects_a_photo_taken_at_another_time():
    """Repair 가 12:49 점심 사진을 아침 체류의 근거로 적고 식사로 바꾼 실제 출력. 근거는 시간과 장소가 모두
    맞아야 해서 거절하고 다시 고르게 한다(사용자 결정 2026-10-30·31)."""

    ctx, photo = _context()
    evening = TimelineEventDraft(
        client_event_id="event-002",
        event_type=EventType.UNKNOWN,
        title="보낸 시간",
        description="{place}에서 시간을 보냈다.",
        start_time=datetime(2026, 6, 20, 16, 0, tzinfo=_KST),
        end_time=datetime(2026, 6, 20, 17, 30, tzinfo=_KST),
        confidence=0.6,
        inference_level=InferenceLevel.DIRECT,
        source_refs=[SourceRef(source_type=EventSourceType.STAY, raw_id=ctx.request.stays[0].raw_id)],
    )
    ctx.draft.events.append(evening)

    [result] = execute_tool_calls(
        ctx,
        [
            RepairToolCall(
                tool="update_event",
                args={"clientEventId": "event-002", "fields": {"eventType": "MEAL", "evidence": ["B1", "P1"]}},
                reason="음식 사진이 있어 식사로 구체화한다",
            )
        ],
    )

    assert not result.ok and "P1" in result.message
    assert evening.event_type is EventType.UNKNOWN, "거절하면 아무것도 바꾸지 않는다."


def test_update_event_will_not_make_a_meal_without_evidence():
    """근거 없이 식사로 바꾸지 않는다(사용자 결정 2026-10-31). Repair 가 없는 사진을 근거로 아침 체류를
    `점심 식사` 로, 검사 결과를 따라 7시간 체류를 식사로 바꾼 실제 출력이다. 식사는 시간이 맞는 사진이나
    결제 알림이 있어야 하고 3시간을 넘지 않는다."""

    ctx, photo = _context()
    stay = ctx.draft.events[0]  # 09:00~18:00 체류, 근거는 체류뿐

    def call(event_id, fields):
        [result] = execute_tool_calls(
            ctx, [RepairToolCall(tool="update_event", args={"clientEventId": event_id, "fields": fields}, reason="식사로")]
        )
        return result

    assert not call("event-001", {"eventType": "MEAL"}).ok, "사진·결제 없이 식사가 되지 않는다."
    assert not call("event-001", {"eventType": "MEAL", "evidence": ["B1", "P1"]}).ok, "9시간 event 는 식사가 아니다."
    assert stay.event_type is EventType.WORK

    lunch = call(
        "event-001",
        {"eventType": "MEAL", "startTime": "2026-06-20T12:20:00+09:00", "endTime": "2026-06-20T12:50:00+09:00", "evidence": ["B1", "P1"]},
    )
    assert lunch.ok, lunch.message


def test_update_event_will_not_write_a_meal_on_an_event_without_evidence():
    """타입을 두고 문장만 식사로 고치는 호출도 같은 근거를 본다. 대화 알림만 가진 아침 체류의 문장이 식사로 덮였다."""

    ctx, _ = _context()

    def call(fields):
        [result] = execute_tool_calls(
            ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": fields}, reason="문장")]
        )
        return result

    assert not call({"title": "대현동에서 식사"}).ok
    assert not call({"description": "{place}에서 음식을 먹었다."}).ok
    assert call({"description": "{place}에서 시간을 보냈다."}).ok


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


def test_split_event_will_not_make_a_meal_piece_without_evidence():
    """split_event 로도 근거 없는 식사 조각을 만들지 않는다(사용자 결정 2026-10-31). 대화 알림만 가진 아침
    체류가 `09:06 식사` 조각이 된 실제 출력. 사진이 있는 시간의 조각은 식사가 될 수 있다."""

    ctx, photo = _context()
    ctx.draft.events[0].source_refs.append(SourceRef(source_type=EventSourceType.PHOTO, raw_id=photo.raw_id))

    def split(meal_start, meal_end):
        [result] = execute_tool_calls(
            ctx,
            [
                RepairToolCall(
                    tool="split_event",
                    args={
                        "clientEventId": "event-001",
                        "parts": [
                            {"startTime": "2026-06-20T09:00:00+09:00", "endTime": meal_start, "eventType": "WORK", "title": "업무", "description": "{place}에서 일했다."},
                            {"startTime": meal_start, "endTime": meal_end, "eventType": "MEAL", "title": "점심", "description": "{place}에서 점심을 먹었다."},
                            {"startTime": meal_end, "endTime": "2026-06-20T18:00:00+09:00", "eventType": "WORK", "title": "업무", "description": "{place}에서 일했다."},
                        ],
                    },
                    reason="점심을 꺼낸다",
                )
            ],
        )
        return result

    assert not split("2026-06-20T09:30:00+09:00", "2026-06-20T09:50:00+09:00").ok, "사진이 없는 시간은 식사가 아니다."
    assert len(ctx.draft.events) == 1, "거절하면 나누지 않는다."
    result = split("2026-06-20T12:20:00+09:00", "2026-06-20T12:50:00+09:00")
    assert result.ok, result.message



def test_update_event_clears_the_writing_mark_only_when_it_writes():
    """코드가 맡긴 event 는 Repair 가 글을 쓰면 끝난다. 시간만 고쳐서는 끝나지 않는다."""

    from app.services.skeleton_guard import NEEDS_WRITING

    ctx, _ = _context()
    event = ctx.draft.events[0]
    event.merge_reason = NEEDS_WRITING

    def call(fields):
        [result] = execute_tool_calls(
            ctx, [RepairToolCall(tool="update_event", args={"clientEventId": "event-001", "fields": fields}, reason="글")]
        )
        assert result.ok, result.message

    call({"endTime": "2026-06-20T17:00:00+09:00"})
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

    report.findings = [{"kind": "DURATION_OVER_LIMIT"}]
    assert not _writing_left(ctx)
