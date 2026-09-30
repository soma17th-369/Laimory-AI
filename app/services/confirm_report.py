"""확정 pass 의 보정 내역 기록 (#119).

확정 pass 는 draft 를 직접 고친다. 그런데 무엇을 고쳤는지는 `"event 2건의 시간을 근거에
맞췄습니다: 제목…"` 같은 warning 문장으로만 남아, Repair 는 **어느 event 의 어느 값이
무엇에서 무엇으로 바뀌었는지** 알 수 없었다. 그래서 코드가 이미 본 것까지 처음부터 다시
검증했고, 정작 보정으로 어색해진 문장은 놓쳤다.

이 모듈은 guard 를 하나씩 돌릴 때마다 직전·직후의 event 를 비교해 그 차이를 적는다.
**guard 는 고치지 않는다.** 옆에서 지켜보며 적기만 하므로 guard 의 동작과 순서는 그대로다.

**코드는 지워도 버리지 않는다.** event 가 사라지면 그 전체 내용을 남긴다. 지워진 event 가
warning 에 제목 세 개나 건수로만 남으면, 하루를 설명하는 데 필요한 내용이 빠져도 Repair 가
알 수 없다.

기록은 draft 가 아니라 Repair 의 작업 상태가 들고 있다. App Server 로 나가는 결과 저장
계약에는 들어가지 않는다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.schemas import TimelineDraft, TimelineEventDraft

Snapshot = dict[str, Any]


def _snapshot(event: TimelineEventDraft) -> Snapshot:
    """비교하고 남길 값. 사용자가 읽는 값과 시간·근거다.

    `uncertainty`·`tags` 는 넣지 않는다. guard 마다 한 줄씩 덧붙여 거의 매번 달라지므로
    넣으면 실제로 다듬어야 할 변화가 그 속에 묻힌다.
    """

    return {
        "eventType": event.event_type.value,
        "title": event.title,
        "description": event.description,
        "place": event.place,
        "address": event.address,
        "startTime": event.start_time.isoformat(),
        "endTime": event.end_time.isoformat(),
        "confidence": event.confidence,
        "sourceRawIds": [str(ref.raw_id) for ref in event.source_refs],
    }


def _changes(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    """두 상태에서 달라진 값만. 근거 목록은 더해진 것과 빠진 것으로 나눠 적는다."""

    changes: dict[str, Any] = {}
    for key, old in before.items():
        new = after[key]
        if old == new:
            continue
        if key == "sourceRawIds":
            changes[key] = {
                "added": [raw_id for raw_id in new if raw_id not in old],
                "removed": [raw_id for raw_id in old if raw_id not in new],
            }
            continue
        changes[key] = {"before": old, "after": new}
    return changes


@dataclass
class _Tracked:
    """한 단계가 시작될 때의 event 하나. 객체를 붙들어 두어 `id()` 가 재사용되지 않게 한다."""

    event: TimelineEventDraft
    client_event_id: str
    snapshot: Snapshot


@dataclass
class _Correction:
    step: str
    event: TimelineEventDraft
    changes: dict[str, Any]


@dataclass
class _Finding:
    kind: str
    event: TimelineEventDraft | None
    #: 값이거나, 기록을 굳힐 때 부를 함수. event 여럿을 `clientEventId` 로 가리키는 검사
    #: 결과는 최종 id 가 매겨진 뒤에 만들어야 하므로 함수로 넘긴다.
    detail: dict[str, Any] | Callable[[], dict[str, Any]]


@dataclass
class ConfirmReport:
    """확정 pass 한 번의 기록.

    `finish` 를 부르기 전까지는 event 객체를 들고 있다가, 부르는 순간 그때의
    `clientEventId` 와 제목으로 굳힌다. 굳히는 이유는 Repair 의 도구가 event 객체를 새것으로
    갈아 끼우기 때문이다 — 객체를 계속 들고 있으면 다음 회차에는 draft 에 없는 event 를
    가리키게 된다.
    """

    #: 몇 번째 확정인가. Repair 가 돌 때마다 하나씩 늘어난다.
    sequence: int = 1
    corrected: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    added: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)

    _corrections: list[_Correction] = field(default_factory=list, repr=False)
    _added: list[tuple[str, TimelineEventDraft]] = field(default_factory=list, repr=False)
    _findings: list[_Finding] = field(default_factory=list, repr=False)

    def run(self, step: str, draft: TimelineDraft, apply: Callable[[], Any]) -> Any:
        """guard 하나를 돌리고 그 전후의 차이를 적는다. guard 의 반환값을 그대로 돌려준다."""

        before = [
            _Tracked(event, event.client_event_id, _snapshot(event))
            for event in draft.events
        ]
        result = apply()
        self._compare(step, before, draft)
        return result

    def add_finding(
        self,
        kind: str,
        detail: dict[str, Any] | Callable[[], dict[str, Any]],
        *,
        event: TimelineEventDraft | None = None,
    ) -> None:
        """코드가 찾았지만 고치지 않은 것. 고치는 것은 Repair 가 한다."""

        self._findings.append(_Finding(kind, event, detail))

    def finish(self, draft: TimelineDraft) -> None:
        """최종 `clientEventId` 가 매겨진 뒤에 불러 기록을 굳힌다."""

        alive = {id(event) for event in draft.events}

        def ident(event: TimelineEventDraft) -> dict[str, Any]:
            # 기록한 뒤 다른 단계가 지운 event 는 id 가 없다. 제목으로만 가리킨다.
            return {
                "clientEventId": event.client_event_id if id(event) in alive else None,
                "title": event.title,
            }

        self.corrected = [
            {**ident(item.event), "step": item.step, "changes": item.changes}
            for item in self._corrections
        ]
        self.added = [
            {**ident(event), "step": step}
            for step, event in self._added
            if id(event) in alive
        ]
        self.findings = [
            {
                "kind": item.kind,
                **(ident(item.event) if item.event is not None else {}),
                **(item.detail() if callable(item.detail) else item.detail),
            }
            for item in self._findings
        ]
        self._corrections, self._added, self._findings = [], [], []

    @property
    def has_content(self) -> bool:
        return bool(self.corrected or self.removed or self.added or self.findings)

    def _compare(self, step: str, before: list[_Tracked], draft: TimelineDraft) -> None:
        by_identity = {id(item.event): item for item in before}
        matched: set[int] = set()
        unmatched_after: list[TimelineEventDraft] = []

        for event in draft.events:
            tracked = by_identity.get(id(event))
            if tracked is None:
                unmatched_after.append(event)
                continue
            matched.add(id(tracked.event))
            self._note(step, tracked, event)

        # 객체를 새것으로 갈아 끼우는 단계가 있다(`filter_draft_sources`). 그런 단계는 id 를
        # 다시 매기지 않으므로 `clientEventId` 로 같은 event 를 찾는다.
        leftover = {
            item.client_event_id: item
            for item in before
            if id(item.event) not in matched
        }
        for event in unmatched_after:
            tracked = leftover.pop(event.client_event_id, None)
            if tracked is None:
                self._added.append((step, event))
                continue
            self._note(step, tracked, event)

        for tracked in leftover.values():
            self.removed.append({"step": step, "event": tracked.snapshot})

    def _note(self, step: str, tracked: _Tracked, event: TimelineEventDraft) -> None:
        changes = _changes(tracked.snapshot, _snapshot(event))
        if changes:
            self._corrections.append(_Correction(step, event, changes))


def reports_to_prompt(reports: list[ConfirmReport]) -> dict[str, Any]:
    """Repair 프롬프트에 실을 형태.

    - `findings` 는 **마지막 확정의 것만** 싣는다. 확정할 때마다 그 draft 로 다시 계산한
      값이라, 앞 회차의 것을 함께 실으면 이미 해소한 위반을 또 고치려 든다.
    - `corrected`·`removed`·`added` 는 몇 번째 확정에서 나온 것인지 붙여 **쌓는다.** 쌓지
      않으면 첫 확정에서 고친 event 의 문장을 다듬을 기회가 한 번뿐이다.
    """

    if not reports:
        return {}

    def tagged(items: list[dict[str, Any]], sequence: int) -> list[dict[str, Any]]:
        return [{"confirm": sequence, **item} for item in items]

    payload: dict[str, Any] = {
        "corrected": [
            item for report in reports for item in tagged(report.corrected, report.sequence)
        ],
        "removed": [
            item for report in reports for item in tagged(report.removed, report.sequence)
        ],
        "added": [
            item for report in reports for item in tagged(report.added, report.sequence)
        ],
        "findings": list(reports[-1].findings),
    }
    return {key: value for key, value in payload.items() if value}
