"""저장 경로까지 가는 테스트용 draft.

event 가 0건인 draft 는 저장 요청 전에 `1211` 로 실패한다(#199). 저장·콜백 흐름을
보려는 테스트는 event 가 하나 있는 draft 를 써야 한다.
"""

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from tests.fixtures.requests import fixture_raw_id


def one_event_draft() -> TimelineDraft:
    """`default_source_items()` 의 STAY 하나를 근거로 한 event 한 건짜리 draft.

    저장 전 자체검증이 근거를 입력에서 찾으므로 실제 입력 rawId 를 가리킨다.
    """

    return TimelineDraft(
        user_id="u-1",
        date="2026-06-20",
        timezone="Asia/Seoul",
        events=[
            TimelineEventDraft(
                client_event_id="event-001",
                event_type=EventType.REST,
                title="집에서 쉬었어요",
                start_time="2026-06-20T10:00:00+09:00",
                end_time="2026-06-20T10:30:00+09:00",
                confidence=0.5,
                inference_level=InferenceLevel.EVIDENCE_BASED,
                source_refs=[
                    SourceRef(
                        source_type=EventSourceType.STAY,
                        raw_id=fixture_raw_id("source-101"),
                    )
                ],
            )
        ],
    )
