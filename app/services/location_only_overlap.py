"""일정·사진 event 와 겹치는 위치만 있는 event 찾기 (#138).

근거가 체류뿐인 event(`카페 체류`, 무관한 알림이 붙어 있어도 같다 — #145)가 같은 시간의 일정·사진 event(`치과 검진`)와 따로 카드로
남는 일이 있다. 같은 방문을 두 번 그린 것이면 체류는 그 event 의 장소 근거로 흡수돼야 한다.
그러나 9시간 근무 체류 안의 1시간 회의처럼 체류가 더 긴 다른 시간이면 둘 다 남아야 한다 —
지우면 하루에 빈칸이 생긴다.

어느 쪽인지는 의미 판단이다. **이 모듈은 고치지 않고 찾기만 한다.** 시간이 겹치고 장소가
어긋나지 않는 짝을 짚어 Repair 에 넘기고, 각 짝에서 일정·사진 event 가 체류 시간을 얼마나
덮는지를 함께 준다. 흡수는 Repair 가 `absorb_location_event` 로 한다. Repair 가 판단하지
못하면 두 카드가 그대로 남는다 — 잃는 것은 없다.

시간은 event 의 시간으로 잰다. 확정 단계가 이미 근거에 맞춰 둔 값이고, Repair 가 보는
것도 그 값이다.
"""

from dataclasses import dataclass
from datetime import timedelta

from app.schemas import TimelineDraft, TimelineDraftRequest, TimelineEventDraft
from app.services.location_link import (
    activity_notification_ids,
    collect_evidence,
    has_event_evidence,
    is_location_only,
    own_place_texts,
    place_compatible,
)


@dataclass(frozen=True)
class OverlapTarget:
    event: TimelineEventDraft
    #: 일정·사진 event 가 위치만 있는 event 의 시간을 덮는 비율(0~1).
    cover_ratio: float


@dataclass(frozen=True)
class LocationOnlyOverlap:
    """위치만 있는 event 하나와, 그것과 겹치는 일정·사진 event 들."""

    event: TimelineEventDraft
    targets: tuple[OverlapTarget, ...]

    def detail(self) -> dict:
        """보고용 값. 최종 `clientEventId` 가 매겨진 뒤에 부른다."""

        return {
            "overlappingEvents": [
                {
                    "clientEventId": target.event.client_event_id,
                    "title": target.event.title,
                    "coverRatio": round(target.cover_ratio, 2),
                }
                for target in self.targets
            ]
        }


def _overlap(left: TimelineEventDraft, right: TimelineEventDraft) -> timedelta:
    return max(
        min(left.end_time, right.end_time) - max(left.start_time, right.start_time),
        timedelta(0),
    )


def _touches(location: TimelineEventDraft, other: TimelineEventDraft) -> bool:
    """시간이 겹치는가. 순간 event(사진)는 체류 시간 안에 있으면 겹친다."""

    if other.start_time == other.end_time:
        return location.start_time <= other.start_time <= location.end_time
    return location.start_time < other.end_time and other.start_time < location.end_time


def find_location_only_overlaps(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> list[LocationOnlyOverlap]:
    """일정·사진 event 와 시간이 겹치고 장소가 어긋나지 않는 위치만 있는 event."""

    evidence = collect_evidence(request)
    stays = {stay.raw_id: stay.item for stay in evidence.stays}
    rich = [event for event in draft.events if has_event_evidence(event)]
    if not rich:
        return []

    activity = activity_notification_ids(request)
    found: list[LocationOnlyOverlap] = []
    for event in draft.events:
        if not is_location_only(event, activity):
            continue
        cited = [stays[ref.raw_id] for ref in event.source_refs if ref.raw_id in stays]
        length = event.end_time - event.start_time
        targets = [
            OverlapTarget(
                other,
                _overlap(event, other) / length if length > timedelta(0) else 1.0,
            )
            for other in rich
            if _touches(event, other)
            and (
                not cited
                or any(
                    place_compatible(own_place_texts(other, evidence), stay)
                    for stay in cited
                )
            )
        ]
        if targets:
            targets.sort(key=lambda target: (-target.cover_ratio, target.event.start_time))
            found.append(LocationOnlyOverlap(event, tuple(targets)))
    return found
