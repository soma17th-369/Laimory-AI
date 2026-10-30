"""근거 묶음 (#169, v4 세트).

위치 뼈대의 구간마다 그 시간의 사진·일정·대화 단서를 붙여 **묶음**을 만든다. Timeline v4 는
묶음을 읽고 event 를 판단하며, 근거를 rawId 대신 묶음·단서 id(`B3`·`P2`·`N5`)로 가리킨다.
`location_link`(#138)의 방향이 뒤집힌 것이다 — 구간이 주인이고 단서가 붙는다.

붙이는 규칙(사용자 결정 2026-10-29). **단서 하나가 여러 구간에 걸치면 걸친 구간 모두에
붙는다.** 어느 구간의 근거인지는 Timeline 이 판단한다.

- 일정: 시간이 겹치는 구간 모두. 배경 일정(종일·12시간 이상)은 자기 묶음이 되고, 안에 든
  구간에는 배경 일정이 있다는 표시만 단다.
- 사진: 촬영 시각을 품는 구간. 촬영 위치가 바로 앞·뒤 구간의 장소와 같으면(250m 이내) 그
  구간에도 붙인다. 구간 경계나 빈 시간에 찍은 사진도 위치가 맞는 구간에 붙는다.
- 대화: 대화가 오간 시간과 겹치는 구간 모두.
- 그 밖의 알림(결제·예약·소식·행동 결과·광고): **시간으로 붙이지 않는다.** 수신 시각은 그
  일이 일어난 때가 아니다. 하루 전체 목록으로 두고 Timeline 이 내용으로 쓸 event 를 고른다.

붙을 구간이 없는 사진·일정·대화는 자기 묶음이 된다. 시간이 가깝다는 이유만으로 구간에 붙이지
않는다(#163). 단독 위치 기록은 시간이 겹치는 묶음에 보태진다.

여러 묶음에 붙은 단서도 최종 event 에서는 기존 규칙을 따른다. 일정은 여러 event 가 함께 가질
수 있고, 사진은 사진 단일 귀속(#119)대로 정확히 한 event 에만 남는다.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.schemas import AgentWarning, EventSourceType, SourceRef, TimelineDraftRequest
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.base_events import AreaNamer, BaseEvent, area_namer, build_base_event
from app.services.calendar_clues import CalendarClue
from app.services.location_metrics import SAME_PLACE_MAX_METERS, distance_between, same_photo_scene
from app.services.location_skeleton import (
    LocationRecord,
    LocationSkeleton,
    SegmentKind,
    SkeletonSegment,
)
from app.services.place_resolver import unique_place_names
from app.services.source_lookup import build_owner_index


@dataclass
class ClueBundle:
    """구간 하나와 거기 붙은 단서. 구간이 없는 묶음은 단서 하나로 된 묶음이다."""

    bundle_id: str
    start: datetime | None
    end: datetime | None
    segment: SkeletonSegment | None = None
    photos: list[PhotoClue] = field(default_factory=list)
    calendars: list[CalendarClue] = field(default_factory=list)
    conversations: list[NotificationClue] = field(default_factory=list)
    isolated: list[LocationRecord] = field(default_factory=list)
    #: 이 구간을 덮는 배경 일정의 id. 배경 일정 자체는 자기 묶음에 있다.
    background_calendar_ids: list[str] = field(default_factory=list)
    #: 구간 묶음의 기본 event(`E1`…). 단서만 든 묶음에는 없다.
    base_event: BaseEvent | None = None

    @property
    def places(self) -> list[str]:
        """이 묶음의 장소 후보 풀. 구간의 이름이 앞이고 사진의 이름이 뒤에 붙는다(#171)."""

        names = list(self.segment.places) if self.segment else []
        names.extend(name for photo in self.photos for name in photo.places)
        return unique_place_names(names)


@dataclass
class TimelineClues:
    """Timeline v4 의 입력 전부. Repair v4 도 같은 값을 본다."""

    skeleton: LocationSkeleton
    calendars: list[CalendarClue]
    photos: list[PhotoClue]
    notifications: list[NotificationClue]
    bundles: list[ClueBundle]
    #: 시간으로 묶음에 붙이지 않는 알림(대화가 아닌 것).
    day_notifications: list[NotificationClue]
    #: 어느 묶음과도 시간이 겹치지 않는 단독 위치 기록.
    loose_records: list[LocationRecord] = field(default_factory=list)
    warnings: list[AgentWarning] = field(default_factory=list)
    #: 같은 장소(250m 이내)의 주소를 하나로 맞춘 표(`place_resolver.same_place_addresses`).
    #: Timeline 이 같은 장소를 시간마다 다른 주소로 읽지 않게 입력의 주소를 이것으로 바꿔 싣는다.
    same_place: dict[str, str] = field(default_factory=dict)

    #: 제목에 쓰는 지명(사용자 결정). 그날 시·도가 둘 이상인지를 안다.
    areas: AreaNamer = field(default_factory=AreaNamer)

    def address(self, address: str | None) -> str | None:
        if not address:
            return address
        return self.same_place.get(address.strip(), address)

    def area(self, address: str | None) -> str | None:
        """그 주소가 속한 장소의 지명. 같은 장소는 같은 지명이다."""

        return self.areas.name(self.address(address))

    def bundle(self, bundle_id: str) -> ClueBundle | None:
        return next((b for b in self.bundles if b.bundle_id == bundle_id), None)

    def base_bundle(self, base_id: str) -> ClueBundle | None:
        """기본 event id 의 묶음."""

        return next(
            (b for b in self.bundles if b.base_event and b.base_event.base_id == base_id), None
        )


def build_timeline_clues(
    skeleton: LocationSkeleton,
    calendars: list[CalendarClue],
    photos: list[PhotoClue],
    notifications: list[NotificationClue],
    *,
    warnings: list[AgentWarning] | None = None,
    same_place: dict[str, str] | None = None,
) -> TimelineClues:
    """뼈대와 단서로 근거 묶음을 만든다."""

    same_place = dict(same_place or {})
    areas = area_namer(_day_addresses(skeleton, calendars, photos))

    def area_of(segment: SkeletonSegment) -> str | None:
        address = segment.address
        return areas.name(same_place.get(address.strip(), address)) if address else None

    segment_bundles = [
        ClueBundle(
            bundle_id="",
            start=segment.start,
            end=segment.end,
            segment=segment,
            base_event=build_base_event(segment, number, area_of(segment)),
        )
        for number, segment in enumerate(skeleton.segments, start=1)
    ]
    extra: list[ClueBundle] = []

    for calendar in calendars:
        if calendar.background or calendar.start is None:
            extra.append(_own(calendar.start, calendar.end, calendars=[calendar]))
            if calendar.background and calendar.start is not None:
                for bundle in _overlapping(segment_bundles, calendar.start, calendar.end):
                    bundle.background_calendar_ids.append(calendar.clue_id)
            continue
        hits = _overlapping(segment_bundles, calendar.start, calendar.end)
        for bundle in hits:
            bundle.calendars.append(calendar)
        if not hits:
            extra.append(_own(calendar.start, calendar.end, calendars=[calendar]))

    for photo in sorted(photos, key=lambda p: (p.taken_at is None, p.taken_at or datetime.min)):
        hits = _photo_bundles(segment_bundles, photo)
        for bundle in hits:
            bundle.photos.append(photo)
        if hits:
            continue
        # 구간에 붙지 않은 사진은 같은 장면이면 한 묶음이다(`same_photo_scene`). 2초 간격 저녁
        # 사진 두 장이 묶음 둘이 되자 Timeline 이 식사 event 를 둘 만들었다.
        scene = next(
            (
                bundle
                for bundle in reversed(extra)
                if bundle.photos
                and not bundle.calendars
                and same_photo_scene(
                    bundle.photos[-1].taken_at,
                    (bundle.photos[-1].latitude, bundle.photos[-1].longitude),
                    photo.taken_at,
                    (photo.latitude, photo.longitude),
                )
            ),
            None,
        )
        if scene is not None:
            scene.photos.append(photo)
            scene.end = photo.taken_at
        else:
            extra.append(_own(photo.taken_at, photo.taken_at, photos=[photo]))

    day_notifications: list[NotificationClue] = []
    for clue in notifications:
        if clue.kind is not NotificationClueKind.CONVERSATION:
            day_notifications.append(clue)
            continue
        start, end = clue.first_at, clue.last_at or clue.first_at
        hits = _overlapping(segment_bundles, start, end) if start else []
        for bundle in hits:
            bundle.conversations.append(clue)
        if not hits:
            extra.append(_own(start, end, conversations=[clue]))

    bundles = sorted(
        [*segment_bundles, *extra],
        key=lambda b: (b.start is None, b.start.timestamp() if b.start else 0.0),
    )
    for number, bundle in enumerate(bundles, start=1):
        bundle.bundle_id = f"B{number}"

    loose: list[LocationRecord] = []
    for record in skeleton.isolated:
        hits = _overlapping(bundles, record.start, record.end)
        for bundle in hits[:1]:
            bundle.isolated.append(record)
        if not hits:
            loose.append(record)

    return TimelineClues(
        skeleton=skeleton,
        calendars=calendars,
        photos=photos,
        notifications=notifications,
        bundles=bundles,
        day_notifications=day_notifications,
        loose_records=loose,
        warnings=list(warnings or []),
        same_place=same_place,
        areas=areas,
    )


def _day_addresses(
    skeleton: LocationSkeleton, calendars: list[CalendarClue], photos: list[PhotoClue]
) -> list[str | None]:
    """그날 사용자가 있었던 곳의 주소. 시·도가 둘 이상인지 보는 데 쓴다."""

    records = [*(r for segment in skeleton.segments for r in segment.records), *skeleton.isolated]
    spots = [spot for r in records for spot in (r.spot, r.origin, r.destination) if spot]
    return [
        *(spot.address for spot in spots),
        *(photo.address for photo in photos),
        *(calendar.address for calendar in calendars),
    ]


def _own(start: datetime | None, end: datetime | None, **clues) -> ClueBundle:
    return ClueBundle(bundle_id="", start=start, end=end, **clues)


def _overlapping(
    bundles: list[ClueBundle], start: datetime | None, end: datetime | None
) -> list[ClueBundle]:
    """시간이 겹치는 묶음. 한 점짜리 단서는 그 시각을 품는 묶음이다."""

    if start is None:
        return []
    end = end or start
    hits = []
    for bundle in bundles:
        if bundle.start is None or bundle.end is None:
            continue
        if start == end:
            if bundle.start <= start <= bundle.end:
                hits.append(bundle)
        elif start < bundle.end and bundle.start < end:
            hits.append(bundle)
    return hits


def _photo_bundles(bundles: list[ClueBundle], photo: PhotoClue) -> list[ClueBundle]:
    """사진이 붙을 구간: 촬영 시각을 품는 구간과, 위치가 같은 바로 앞·뒤 구간."""

    if photo.taken_at is None:
        return []
    containing = _overlapping(bundles, photo.taken_at, photo.taken_at)
    if containing:
        index = bundles.index(containing[0])
        neighbors = [bundles[i] for i in (index - 1, index + 1) if 0 <= i < len(bundles)]
    else:
        before = [b for b in bundles if b.end is not None and b.end < photo.taken_at]
        after = [b for b in bundles if b.start is not None and b.start > photo.taken_at]
        neighbors = [*before[-1:], *after[:1]]
    hits = list(containing)
    for bundle in neighbors:
        if bundle not in hits and _same_spot(photo, bundle.segment):
            hits.append(bundle)
    return sorted(hits, key=bundles.index)


def _same_spot(photo: PhotoClue, segment: SkeletonSegment | None) -> bool:
    if segment is None or photo.latitude is None or photo.longitude is None:
        return False
    points = [segment.spot.point] if segment.spot and segment.spot.point else []
    if segment.kind is SegmentKind.STAY:
        points.extend(r.spot.point for r in segment.records if r.spot and r.spot.point)
    for point in points:
        distance = distance_between((photo.latitude, photo.longitude), point)
        if distance is not None and distance <= SAME_PLACE_MAX_METERS:
            return True
    return False


# --- 근거 id 펼치기 -----------------------------------------------------------------

#: 단서 종류마다 `sourceRefs.reason` 에 코드가 적는 말. 왜 묶였는지는 `mergeReason` 이 담는다.
_REASONS = {
    "segment": "위치 구간",
    "isolated": "짧은 위치 기록",
    "photo": "사진",
    "calendar": "일정",
    "conversation": "대화 알림",
    "notification": "알림",
}


def expand_evidence(
    clues: TimelineClues,
    events_evidence: list[list[str]],
    request: TimelineDraftRequest,
    events_bases: list[list[str]] | None = None,
) -> list[list[SourceRef]]:
    """event 마다 근거 id 목록을 `sourceRefs` 로 펼친다.

    기본 event id(`events_bases`)는 그 구간의 위치 기록이다. 묶음 id 는 그 구간과 거기 붙은 단서
    전부다. 다만 **다른 event 가 단서 id 로 직접 가져간 단서는 그 묶음을 펼칠 때 빠진다** — 묶음에 붙은 점심 사진을 다른 event 가 `P2` 로 가져갔으면
    그 묶음의 event 에는 넣지 않는다. 같은 event 안에서 같은 rawId 는 한 번만 담는다.
    모르는 id 는 버린다.
    """

    by_clue_id = _clue_index(clues)
    taken_directly = {
        identifier
        for evidence in events_evidence
        for identifier in evidence
        if identifier in by_clue_id
    }
    owners = build_owner_index(request)

    expanded: list[list[SourceRef]] = []
    bases_by_event = events_bases or [[] for _ in events_evidence]
    for evidence, bases in zip(events_evidence, bases_by_event):
        refs: list[SourceRef] = []
        seen: set[str] = set()

        def add(raw_ids: list[str], kind: str) -> None:
            for raw_id in raw_ids:
                if raw_id in seen:
                    continue
                source_type = _source_type(owners, raw_id)
                if source_type is None:
                    continue
                seen.add(raw_id)
                refs.append(
                    SourceRef(source_type=source_type, raw_id=raw_id, reason=_REASONS[kind])
                )

        # 기본 event 에서 온 event 는 그 구간의 위치 기록을 근거로 갖는다. 구간에 붙은 단서는
        # evidence 에 적은 것만 담는다 — 나눈 조각마다 묶음의 사진을 모두 갖지 않게 한다.
        for base_id in bases:
            bundle = clues.base_bundle(base_id.strip())
            if bundle is not None and bundle.segment is not None:
                add(bundle.segment.raw_ids, "segment")
        for identifier in evidence:
            identifier = identifier.strip()
            if identifier in by_clue_id:
                kind, raw_ids = by_clue_id[identifier]
                add(raw_ids, kind)
                continue
            bundle = clues.bundle(identifier)
            if bundle is None:
                continue
            if bundle.segment is not None:
                add(bundle.segment.raw_ids, "segment")
            add([r.raw_id for r in bundle.isolated], "isolated")
            for kind, items in (
                ("photo", bundle.photos),
                ("calendar", bundle.calendars),
                ("conversation", bundle.conversations),
            ):
                for clue in items:
                    if clue.clue_id in taken_directly and clue.clue_id not in evidence:
                        continue
                    add(_raw_ids_of(clue), kind)
        expanded.append(refs)
    return expanded


def _clue_index(clues: TimelineClues) -> dict[str, tuple[str, list[str]]]:
    index: dict[str, tuple[str, list[str]]] = {}
    for photo in clues.photos:
        index[photo.clue_id] = ("photo", [photo.raw_id])
    for calendar in clues.calendars:
        index[calendar.clue_id] = ("calendar", [calendar.raw_id])
    for clue in clues.notifications:
        kind = "conversation" if clue.kind is NotificationClueKind.CONVERSATION else "notification"
        index[clue.clue_id] = (kind, list(clue.raw_ids))
    return index


def _raw_ids_of(clue: PhotoClue | CalendarClue | NotificationClue) -> list[str]:
    if isinstance(clue, NotificationClue):
        return list(clue.raw_ids)
    return [clue.raw_id]


def _source_type(owners: dict[str, set[EventSourceType]], raw_id: str) -> EventSourceType | None:
    """입력이 가진 실제 타입. 입력에 없거나 타입이 둘 이상이면 근거로 담지 않는다."""

    types = owners.get(raw_id)
    if not types or len(types) != 1:
        return None
    return next(iter(types))


# --- 프롬프트 입력 -------------------------------------------------------------------
#
# 좌표·rawId 는 싣지 않는다(#80). 이동 방식·속도도 싣지 않는다 — 이동수단은 문장에 쓰지 않고
# 산책 판정은 뼈대가 이미 했다(L28). 값이 없는 키는 뺀다(빈 값에 의미를 붙이지 않게).


def timeline_input_sections(clues: TimelineClues) -> dict[str, object]:
    """Timeline·Repair v4 가 읽는 `[하루]`·`[근거 묶음]`·`[빈 시간]`·`[알림]`."""

    skeleton = clues.skeleton
    day: dict[str, object] = {"hasLocation": skeleton.has_location}
    if skeleton.first_observed_at:
        day["firstObservedAt"] = skeleton.first_observed_at.isoformat()
    if skeleton.last_observed_at:
        day["lastObservedAt"] = skeleton.last_observed_at.isoformat()
    if skeleton.last_spot and (label := skeleton.last_spot.label):
        day["lastPlace"] = label
    return {
        "day": day,
        "bundles": [_bundle_dict(bundle, clues) for bundle in clues.bundles],
        "emptyTimes": [_empty_dict(empty, clues) for empty in skeleton.empty_times],
        "notifications": [_notification_dict(clue) for clue in clues.day_notifications],
    }


def _bundle_dict(bundle: ClueBundle, clues: TimelineClues) -> dict[str, object]:
    payload: dict[str, object] = {"id": bundle.bundle_id}
    if bundle.start is not None:
        payload["startAt"] = bundle.start.isoformat()
    if bundle.end is not None:
        payload["endAt"] = bundle.end.isoformat()
    if bundle.base_event is not None:
        payload["baseEvent"] = bundle.base_event.as_prompt_dict()
    if bundle.segment is not None:
        payload["location"] = _segment_dict(bundle.segment, clues)
    if bundle.places:
        payload["places"] = bundle.places
    if bundle.background_calendar_ids:
        payload["backgroundCalendars"] = list(bundle.background_calendar_ids)
    if bundle.calendars:
        payload["calendars"] = [
            {**calendar.as_prompt_dict(), **({"area": a} if (a := clues.area(calendar.address)) else {})}
            for calendar in bundle.calendars
        ]
    if bundle.photos:
        payload["photos"] = [_photo_dict(photo, clues) for photo in bundle.photos]
    if bundle.conversations:
        payload["conversations"] = [_notification_dict(clue) for clue in bundle.conversations]
    if bundle.isolated:
        payload["shortRecords"] = [_record_dict(record, clues) for record in bundle.isolated]
    return payload


def _segment_dict(segment: SkeletonSegment, clues: TimelineClues) -> dict[str, object]:
    payload: dict[str, object] = {"kind": segment.kind.value}
    if segment.address:
        payload["address"] = clues.address(segment.address)
        if segment.kind is not SegmentKind.MOVEMENT and (area := clues.area(segment.address)):
            payload["area"] = area
    if segment.kind is SegmentKind.STAY:
        if len(segment.records) > 1:
            payload["records"] = [_record_dict(record, clues) for record in segment.records]
        if segment.internal_gaps:
            payload["collectionGaps"] = [
                {"startAt": gap.start.isoformat(), "endAt": gap.end.isoformat()}
                for gap in segment.internal_gaps
            ]
        return payload
    for key, spot in (("from", segment.origin), ("to", segment.destination)):
        if spot is not None:
            payload[key] = _spot_dict(spot, clues)
    if segment.distance_meters is not None:
        payload["distanceMeters"] = round(segment.distance_meters)
    if segment.round_trip:
        payload["roundTrip"] = True
    if segment.region_changed is not None:
        payload["regionChanged"] = segment.region_changed
    return payload


def _record_dict(record: LocationRecord, clues: TimelineClues) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": record.kind,
        "startAt": record.start.isoformat(),
        "endAt": record.end.isoformat(),
    }
    spot = record.spot if record.is_stay else record.destination
    if spot is not None:
        payload.update(_spot_dict(spot, clues))
    return payload


def _spot_dict(spot, clues: TimelineClues) -> dict[str, object]:
    payload: dict[str, object] = {}
    if spot.places:
        payload["places"] = list(spot.places[:3])
    if spot.address:
        payload["address"] = clues.address(spot.address)
        if area := clues.area(spot.address):
            payload["area"] = area
    return payload


def _photo_dict(photo: PhotoClue, clues: TimelineClues) -> dict[str, object]:
    payload: dict[str, object] = {"id": photo.clue_id}
    if photo.taken_at is not None:
        payload["takenAt"] = photo.taken_at.isoformat()
    for key, value in (
        ("activity", photo.activity),
        ("description", photo.description),
        ("address", clues.address(photo.address)),
        ("area", clues.area(photo.address)),
    ):
        if value:
            payload[key] = value
    if photo.places:
        payload["places"] = list(photo.places)
    if photo.uncertainty:
        payload["uncertainty"] = list(photo.uncertainty)
    return payload


def _notification_dict(clue: NotificationClue) -> dict[str, object]:
    """알림 단서. 대화는 오간 시간을 싣고, 그 밖의 알림은 시각을 싣지 않는다(사용자 결정)."""

    payload: dict[str, object] = {"id": clue.clue_id}
    if clue.kind is None:
        payload["unread"] = True
    else:
        payload["kind"] = clue.kind.value
    if clue.app_names:
        payload["appNames"] = list(clue.app_names)
    for key, value in (("counterpart", clue.counterpart), ("summary", clue.summary)):
        if value:
            payload[key] = value
    if clue.schedule_today is not None:
        payload["scheduleToday"] = clue.schedule_today
    if clue.kind is NotificationClueKind.CONVERSATION:
        if clue.first_at is not None:
            payload["firstAt"] = clue.first_at.isoformat()
        if clue.last_at is not None:
            payload["lastAt"] = clue.last_at.isoformat()
    return payload


def _empty_dict(empty, clues: TimelineClues) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": empty.kind.value,
        "startAt": empty.start.isoformat(),
        "endAt": empty.end.isoformat(),
    }
    for key, spot in (("before", empty.before), ("after", empty.after)):
        if spot is not None:
            payload[key] = _spot_dict(spot, clues)
    if empty.same_place is not None:
        payload["samePlace"] = empty.same_place
    return payload
