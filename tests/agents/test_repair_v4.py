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
from app.schemas.clues import PhotoClue
from app.services.clue_bundles import build_timeline_clues
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.requests import make_request, photo_item, stay_item

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
