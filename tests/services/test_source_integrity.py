"""LLM이 만든 rawId를 실제 요청 allowlist와 대조하는 경계 검증."""

import logging

from app.agents.events.base_event_agent import EventAgent
from app.core.execution_context import ExecutionStage, execution_scope
from app.schemas import AgentEventResult, TimelineDraft, TimelineEventDraft
from app.services.draft_repair import repair_draft
from app.services.source_integrity import (
    filter_agent_result_sources,
    filter_draft_sources,
)
from tests.fixtures.requests import fixture_raw_id, make_request, stay_item

VALID_RAW_ID = fixture_raw_id("integrity-valid")
UNKNOWN_RAW_ID = fixture_raw_id("integrity-unknown")


def _candidate(title: str, raw_ids: list[str]) -> dict:
    return {
        "eventType": "REST",
        "timeRange": {
            "startTime": "2026-06-20T09:00:00+09:00",
            "endTime": "2026-06-20T10:00:00+09:00",
        },
        "title": title,
        "description": "",
        "sourceRefs": [
            {"sourceType": "STAY", "rawId": raw_id} for raw_id in raw_ids
        ],
        "confidence": 0.7,
        "inferenceLevel": "EVIDENCE_BASED",
        "uncertainty": [],
    }


def _event(title: str, raw_ids: list[str]) -> TimelineEventDraft:
    return TimelineEventDraft.model_validate(
        {
            "clientEventId": title,
            "eventType": "REST",
            "title": title,
            "startTime": "2026-06-20T09:00:00+09:00",
            "endTime": "2026-06-20T10:00:00+09:00",
            "confidence": 0.7,
            "inferenceLevel": "EVIDENCE_BASED",
            "sourceRefs": [
                {"sourceType": "STAY", "rawId": raw_id} for raw_id in raw_ids
            ],
        }
    )


def _request():
    return make_request(stays=[stay_item(1, raw_id=VALID_RAW_ID)])


def test_event_agent_filter_removes_unknown_refs_and_unsupported_items():
    result = AgentEventResult.model_validate(
        {
            "candidates": [
                _candidate("부분 정상", [VALID_RAW_ID, UNKNOWN_RAW_ID]),
                _candidate("전부 환각", [UNKNOWN_RAW_ID]),
            ],
            "fragments": [
                {
                    "sourceType": "STAY",
                    "rawId": UNKNOWN_RAW_ID,
                    "summary": "환각 단서",
                }
            ],
        }
    )

    filtered, stats = filter_agent_result_sources(result, _request())

    assert [candidate.title for candidate in filtered.candidates] == ["부분 정상"]
    assert [ref.raw_id for ref in filtered.candidates[0].source_refs] == [
        VALID_RAW_ID
    ]
    assert filtered.fragments == []
    assert stats.removed_refs == 3
    assert stats.dropped_items == 2


def test_draft_filter_drops_only_events_without_valid_evidence():
    draft = TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[
            _event("부분 정상", [VALID_RAW_ID, UNKNOWN_RAW_ID]),
            _event("전부 환각", [UNKNOWN_RAW_ID]),
        ],
    )

    stats = filter_draft_sources(draft, _request())

    assert [event.title for event in draft.events] == ["부분 정상"]
    assert [ref.raw_id for ref in draft.events[0].source_refs] == [VALID_RAW_ID]
    assert stats.removed_refs == 2
    assert stats.dropped_items == 1
    assert any("입력에 없는 rawId 참조" in warning.message for warning in draft.warnings)


class _IntegrityEventAgent(EventAgent):
    name = "integrity-test"

    def __init__(self, result: AgentEventResult) -> None:
        self.result = result

    def _generate(self, request):
        return self.result


def _violation_record(caplog):
    return next(
        record
        for record in caplog.records
        if record.getMessage() == "입력에 없는 rawId 참조 정리"
    )


def test_event_agent_logs_recovered_source_violation_without_raw_ids(caplog):
    result = AgentEventResult.model_validate(
        {
            "candidates": [
                _candidate("부분 정상", [VALID_RAW_ID, UNKNOWN_RAW_ID]),
            ],
            "fragments": [],
        }
    )

    with caplog.at_level(logging.DEBUG, logger="app.agents.events.base_event_agent"):
        _IntegrityEventAgent(result).generate(_request())

    record = _violation_record(caplog)
    assert record.fields == {
        "validationCode": "SOURCE_RAW_ID_NOT_IN_REQUEST",
        "itemKind": "CANDIDATE_OR_FRAGMENT",
        "removedRefCount": 1,
        "droppedItemCount": 0,
    }
    # rawId 원문은 사용자 데이터라 운영 로그로 나가지 않는다.
    assert VALID_RAW_ID not in str(record.fields)
    assert UNKNOWN_RAW_ID not in str(record.fields)


def test_repair_logs_dropped_event_without_raw_ids(caplog):
    draft = TimelineDraft(
        user_id="u",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[_event("전부 환각", [UNKNOWN_RAW_ID])],
    )

    with caplog.at_level(logging.DEBUG, logger="app.services.draft_repair"):
        with execution_scope(ExecutionStage.REPAIR_AGENT, agent="repair"):
            repair_draft(draft, _request())

    record = _violation_record(caplog)
    assert record.fields == {
        "validationCode": "SOURCE_RAW_ID_NOT_IN_REQUEST",
        "itemKind": "TIMELINE_EVENT",
        "removedRefCount": 1,
        "droppedItemCount": 1,
    }
    assert UNKNOWN_RAW_ID not in str(record.fields)
