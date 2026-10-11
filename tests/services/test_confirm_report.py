"""확정 pass 의 보정 내역 기록 (#119).

코드가 draft 를 고치면 무엇을 고쳤는지는 warning 문장으로만 남았다. 어느 event 의 어느
값이 무엇에서 무엇으로 바뀌었는지가 없어 Repair 는 전체를 다시 검증했다. 여기서는 그
기록이 실제 보정과 맞는지 본다.

기록은 guard 를 고치지 않고 옆에서 적는다. 그래서 기록을 켜도 끄더라도 확정 결과가
같은지도 함께 확인한다.
"""

from app.schemas import (
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineDraft,
    TimelineEventDraft,
)
from app.services.confirm_report import ConfirmReport, reports_to_prompt
from app.services.draft_repair import repair_draft
from tests.fixtures.requests import (
    fixture_raw_id,
    make_request,
    movement_item,
    photo_item,
    stay_item,
)

DAY = "2026-06-20"


def _t(clock: str) -> str:
    return f"{DAY}T{clock}:00+09:00"


def _event(
    client_event_id: str,
    start: str,
    end: str,
    *refs: tuple[EventSourceType, str],
    event_type=EventType.REST,
    title: str | None = None,
    description: str = "설명",
    confidence: float = 0.7,
    place: str | None = None,
) -> TimelineEventDraft:
    return TimelineEventDraft(
        client_event_id=client_event_id,
        event_type=event_type,
        title=title or client_event_id,
        description=description,
        start_time=_t(start),
        end_time=_t(end),
        confidence=confidence,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        place=place,
        source_refs=[
            SourceRef(source_type=source_type, raw_id=fixture_raw_id(raw_id))
            for source_type, raw_id in refs
        ],
    )


def _draft(*events) -> TimelineDraft:
    return TimelineDraft(
        user_id="u", date=DAY, timezone="Asia/Seoul", events=list(events)
    )


def _request():
    return make_request(
        stays=[
            stay_item(
                1,
                raw_id="stay-cafe",
                start=f"{DAY}T12:00:00",
                end=f"{DAY}T13:00:00",
                place="카페",
                places=["카페"],
            )
        ]
    )


STAY = (EventSourceType.STAY, "stay-cafe")


# --- 고친 것 --------------------------------------------------------------------


def test_correction_names_the_event_the_step_and_the_values():
    """근거에 없는 시각을 적은 event 를 코드가 근거 구간에 맞춘다."""

    draft = _draft(_event("event-009", "11:30", "13:00", STAY, title="카페"))
    report = ConfirmReport()

    repair_draft(draft, _request(), report=report)

    aligned = [item for item in report.corrected if item["step"] == "근거 구간 정렬"]
    assert aligned == [
        {
            # 기록은 최종 id 로 event 를 가리킨다. 확정이 끝나면 id 가 다시 매겨진다.
            "clientEventId": "event-001",
            "title": "카페",
            "step": "근거 구간 정렬",
            "changes": {
                "startTime": {
                    "before": "2026-06-20T11:30:00+09:00",
                    "after": "2026-06-20T12:00:00+09:00",
                }
            },
        }
    ]


def test_untouched_event_is_not_recorded():
    draft = _draft(_event("event-001", "12:00", "13:00", STAY, place="카페"))
    report = ConfirmReport()

    repair_draft(draft, _request(), report=report)

    assert [item for item in report.corrected if "startTime" in item["changes"]] == []
    assert report.removed == []


def test_source_ref_changes_are_recorded_as_added_and_removed():
    """어느 event 에도 없던 사진을 코드가 붙인다."""

    request = make_request(
        stays=_request().stays,
        photos=[photo_item("1", taken=f"{DAY}T12:30:00")],
    )
    draft = _draft(_event("event-001", "12:00", "13:00", STAY, place="카페"))
    report = ConfirmReport()

    repair_draft(draft, request, report=report)

    (photo,) = [item for item in report.corrected if item["step"] == "사진 단일 귀속"]
    assert photo["changes"]["sourceRawIds"] == {
        "added": [fixture_raw_id("photo-1")],
        "removed": [],
    }


# --- 사라진 것 -------------------------------------------------------------------


def test_removed_event_keeps_its_whole_content():
    """지워진 event 가 제목 세 개나 건수로만 남으면 빠진 내용을 알 수 없다."""

    draft = _draft(
        _event("event-001", "12:00", "13:00", STAY, place="카페"),
        _event(
            "event-002",
            "12:10",
            "12:40",
            (EventSourceType.STAY, "not-in-input"),
            title="지어낸 근거",
            description="입력에 없는 근거로 만든 event.",
        ),
    )
    report = ConfirmReport()

    repair_draft(draft, _request(), report=report)

    (removed,) = report.removed
    assert removed["step"] == "입력에 없는 근거 정리"
    assert removed["event"]["title"] == "지어낸 근거"
    assert removed["event"]["description"] == "입력에 없는 근거로 만든 event."
    assert removed["event"]["startTime"] == "2026-06-20T12:10:00+09:00"


def test_merged_event_leaves_the_absorbed_side_behind():
    """병합에서 confidence 가 낮은 쪽의 문장은 사라진다. 그 문장을 기록이 갖고 있다."""

    draft = _draft(
        _event("event-001", "12:00", "12:40", STAY, title="커피", description="커피를 마셨어요.", confidence=0.9, place="카페"),
        _event("event-002", "12:20", "13:00", STAY, title="코드 리뷰", description="코드 리뷰를 했어요.", confidence=0.5, place="카페"),
    )
    report = ConfirmReport()

    repair_draft(draft, _request(), report=report)

    assert [event.title for event in draft.events] == ["커피"]
    absorbed = [item["event"] for item in report.removed]
    assert [item["title"] for item in absorbed] == ["코드 리뷰"]
    assert absorbed[0]["description"] == "코드 리뷰를 했어요."


# --- 찾은 것 --------------------------------------------------------------------


def test_findings_point_at_the_final_client_event_id():
    request = make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=f"{DAY}T08:00:00", end=f"{DAY}T09:00:00"),
            movement_item(2, raw_id="move-back", start=f"{DAY}T18:00:00", end=f"{DAY}T19:00:00"),
        ],
        stays=[
            stay_item(1, raw_id="stay-office", start=f"{DAY}T09:00:00", end=f"{DAY}T18:00:00", place="회사", places=["회사"]),
            stay_item(2, raw_id="stay-home", start=f"{DAY}T06:00:00", end=f"{DAY}T07:30:00", place="집", places=["집"]),
        ],
    )
    draft = _draft(
        _event(
            "event-007",
            "08:00",
            "19:00",
            (EventSourceType.MOVEMENT, "move-out"),
            (EventSourceType.STAY, "stay-office"),
            (EventSourceType.MOVEMENT, "move-back"),
            event_type=EventType.MOVEMENT,
            title="회사에 다녀옴",
        ),
        _event("event-003", "06:00", "07:30", (EventSourceType.STAY, "stay-home"), title="집"),
    )
    report = ConfirmReport()

    repair_draft(draft, request, report=report)

    (finding,) = [item for item in report.findings if item["kind"] == "LONG_STAY_BETWEEN_MOVEMENTS"]
    assert finding["clientEventId"] == "event-002"
    assert finding["title"] == "회사에 다녀옴"
    assert [stay["minutes"] for stay in finding["longStays"]] == [540]
    assert [
        (segment["kind"], segment["startAt"], segment["endAt"])
        for segment in finding["segments"]
    ] == [
        ("MOVEMENT", _t("08:00"), _t("09:00")),
        ("STAY", _t("09:00"), _t("18:00")),
        ("MOVEMENT", _t("18:00"), _t("19:00")),
    ]


def test_movement_event_over_its_type_limit_is_not_a_duration_finding():
    """이동은 지속시간 검사에서 빠진다. 구조 검사가 따로 잡는다."""

    draft = _draft(
        _event("event-001", "12:00", "13:00", STAY, event_type=EventType.MOVEMENT)
    )
    report = ConfirmReport()

    repair_draft(draft, _request(), report=report)

    assert report.findings == []


# --- 확정 결과 ------------------------------------------------------------------


def test_legacy_sets_get_none_of_the_new_checks():
    """v2 는 운영 세트다. 나눌 도구도 읽는 법도 없는 세트에 새 warning 을 보이지 않는다.

    실제 LLM 으로 돌렸을 때 v2 Repair 는 "나눠야 합니다"를 보고 Timeline 재실행을 두 번
    불렀고 위반은 그대로 남았다.
    """

    request = make_request(
        movements=[
            movement_item(1, raw_id="move-out", start=f"{DAY}T08:00:00", end=f"{DAY}T09:00:00"),
            movement_item(2, raw_id="move-back", start=f"{DAY}T18:00:00", end=f"{DAY}T19:00:00"),
        ],
        stays=[
            stay_item(1, raw_id="stay-office", start=f"{DAY}T09:00:00", end=f"{DAY}T18:00:00", place="회사", places=["회사"]),
        ],
    )

    def build() -> TimelineDraft:
        return _draft(
            _event(
                "event-001",
                "08:00",
                "19:00",
                (EventSourceType.MOVEMENT, "move-out"),
                (EventSourceType.STAY, "stay-office"),
                (EventSourceType.MOVEMENT, "move-back"),
                event_type=EventType.MOVEMENT,
                # v2 는 빈 place 를 후보로 채우고 v3 는 비워 둔다(#150). 이 테스트가 보는
                # 것은 검사이므로 place 를 미리 정해 두 확정의 차이를 검사로 좁힌다.
                place="회사",
            )
        )

    legacy_report, report = ConfirmReport(), ConfirmReport()
    legacy = repair_draft(build(), request, report=legacy_report, extended=False)
    current = repair_draft(build(), request, report=report)

    assert legacy_report.findings == []
    assert not [w for w in legacy.warnings if w.warning_id.startswith("warning-movement-stay-")]
    assert [item["kind"] for item in report.findings] == ["LONG_STAY_BETWEEN_MOVEMENTS"]
    assert [w for w in current.warnings if w.warning_id.startswith("warning-movement-stay-")]
    # 고치는 단계는 세트와 무관하게 같다.
    assert legacy.events == current.events


def test_recording_does_not_change_what_repair_does():
    def build() -> TimelineDraft:
        return _draft(
            _event("event-002", "11:30", "13:00", STAY, title="카페"),
            _event("event-001", "12:10", "12:40", (EventSourceType.STAY, "not-in-input")),
        )

    plain = repair_draft(build(), _request())
    recorded = repair_draft(build(), _request(), report=ConfirmReport())

    assert recorded == plain


# --- 프롬프트에 싣는 형태 ---------------------------------------------------------


def _report(sequence: int, *, corrected=(), findings=()) -> ConfirmReport:
    report = ConfirmReport(sequence=sequence)
    report.corrected = list(corrected)
    report.findings = list(findings)
    return report


def test_prompt_keeps_corrections_from_every_confirm():
    """쌓지 않으면 첫 확정에서 고친 event 의 문장을 다듬을 기회가 한 번뿐이다."""

    payload = reports_to_prompt(
        [
            _report(1, corrected=[{"clientEventId": "event-001", "step": "식사 시간"}]),
            _report(2, corrected=[{"clientEventId": "event-004", "step": "장소 확정"}]),
        ]
    )

    assert [(item["confirm"], item["clientEventId"]) for item in payload["corrected"]] == [
        (1, "event-001"),
        (2, "event-004"),
    ]


def test_prompt_carries_only_the_latest_findings():
    """앞 회차의 것을 함께 실으면 이미 해소한 위반을 또 고치려 든다."""

    payload = reports_to_prompt(
        [
            _report(1, findings=[{"kind": "DURATION_OVER_LIMIT", "clientEventId": "event-001"}]),
            _report(2, findings=[{"kind": "DURATION_OVER_LIMIT"}]),
        ]
    )

    assert payload["findings"] == [{"kind": "DURATION_OVER_LIMIT"}]


def test_prompt_drops_resolved_findings():
    payload = reports_to_prompt(
        [
            _report(1, findings=[{"kind": "DURATION_OVER_LIMIT"}]),
            _report(2),
        ]
    )

    assert "findings" not in payload


def test_empty_reports_give_an_empty_payload():
    assert reports_to_prompt([]) == {}
    assert reports_to_prompt([ConfirmReport()]) == {}
