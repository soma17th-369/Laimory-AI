"""근거 묶음 (#169, v4 세트).

위치 뼈대의 구간마다 그 시간의 사진·일정·알림 단서를 붙여 **묶음**을 만든다. Timeline v4 는
묶음을 읽고 event 를 판단하며, 근거를 rawId 대신 묶음·단서 id(`B3`·`P2`·`N5`)로 가리킨다.
`location_link`(#138)의 방향이 뒤집힌 것이다 — 구간이 주인이고 단서가 붙는다.

붙이는 규칙(사용자 결정 2026-10-29). **단서 하나가 여러 구간에 걸치면 걸친 구간 모두에
붙는다.** 어느 구간의 근거인지는 Timeline 이 판단한다.

- 일정: 시간이 겹치는 구간 모두. 배경 일정(종일·12시간 이상)은 자기 묶음이 되고, 안에 든
  구간에는 배경 일정이 있다는 표시만 단다.
- 사진: 촬영 시각을 품는 구간. 촬영 위치가 바로 앞·뒤 구간의 장소와 같으면(250m 이내) 그
  구간에도 붙인다. 구간 경계나 빈 시간에 찍은 사진도 위치가 맞는 구간에 붙는다.
- 대화: **구간에 붙이지 않고 언제나 자기 묶음이다**(#173, 사용자 결정 2026-10-31). "대화는 기존
  이벤트와 연관이 크게 없다." 구간에 붙여 두자 Timeline 이 대화로 체류를 채워 `덕충동에서 대화` 가
  됐다. 대화는 단독 event 로 하루 3개까지 남는다.
- 그 밖의 알림(결제·예약·소식·행동 결과): 처음 받은 시각을 품는 구간 **하나**에 붙인다(#173, 사용자
  결정 2026-10-31). 시각이 없던 때 이 알림은 근거로 거의 쓰이지 않았다(토스·문자 72건 중 2건). 어느
  event 의 것인지는 Timeline·Repair 가 내용으로 판단한다. 묶음 id 를 펼쳐도 알림은 담기지 않고 `N5` 처럼
  직접 적은 알림만 근거다 — 묶음 id 로 따라오게 두자 온라인 결제·설문 알림이 그 시각의 체류에 붙었다.
  구간 밖 시각의 알림과 다른 날 일정을 말하는 알림은 붙이지 않고 하루 목록으로 둔다.

붙을 구간이 없는 사진·일정은 자기 묶음이 된다. 시간이 가깝다는 이유만으로 구간에 붙이지
않는다(#163). 단독 위치 기록은 시간이 겹치는 묶음에 보태진다.

여러 묶음에 붙은 단서도 최종 event 에서는 기존 규칙을 따른다. 일정은 여러 event 가 함께 가질
수 있고, 사진은 사진 단일 귀속(#119)대로 정확히 한 event 에만 남는다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.schemas import AgentWarning, EventSourceType, SourceRef, TimelineDraftRequest
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.base_events import (
    AreaNamer,
    BaseEvent,
    area_namer,
    build_base_event,
    with_calendar,
)
from app.services.calendar_clues import CalendarClue
from app.services.location_metrics import (
    SAME_PLACE_MAX_METERS,
    SHORT_STAY_MAX,
    distance_between,
    same_photo_scene,
)
from app.services.location_skeleton import (
    LocationRecord,
    LocationSkeleton,
    SegmentKind,
    SkeletonSegment,
)
from app.services.place_resolver import unique_place_names
from app.services.source_lookup import build_owner_index, raw_id_of


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
    #: 이 구간의 시각에 받은, 대화가 아닌 알림(#173).
    notifications: list[NotificationClue] = field(default_factory=list)
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
    #: 어느 구간에도 붙지 않은, 대화가 아닌 알림.
    day_notifications: list[NotificationClue]
    #: 어느 묶음과도 시간이 겹치지 않는 단독 위치 기록.
    loose_records: list[LocationRecord] = field(default_factory=list)
    warnings: list[AgentWarning] = field(default_factory=list)
    #: 같은 장소(250m 이내)의 주소를 하나로 맞춘 표(`place_resolver.same_place_addresses`).
    #: Timeline 이 같은 장소를 시간마다 다른 주소로 읽지 않게 입력의 주소를 이것으로 바꿔 싣는다.
    same_place: dict[str, str] = field(default_factory=dict)

    #: 제목에 쓰는 지명(사용자 결정). 그날 시·도가 둘 이상인지를 안다.
    areas: AreaNamer = field(default_factory=AreaNamer)

    @property
    def short_record_ids(self) -> frozenset[str]:
        """단독 위치 기록의 rawId. event 를 만들지 않고 보태기만 하는 근거다."""

        return frozenset(record.raw_id for record in self.skeleton.isolated)

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


def place_only_raw_ids(request: TimelineDraftRequest, clues: TimelineClues) -> set[str]:
    """어디에 있었는지만 말하는 근거의 rawId. 체류·이동 기록과 오늘 일이 아닌 알림이다.

    오늘 일이 아닌 알림(`scheduleToday` 가 거짓)은 그 체류에서 한 일을 말하지 않는다. 다음 주 예매 알림이 온
    영화관 체류를 Timeline 이 관람으로 썼다(#172).
    """

    return {raw_id_of(item) for item in [*request.stays, *request.movements]} | {
        raw_id
        for clue in clues.notifications
        if clue.schedule_today is False
        for raw_id in clue.raw_ids
    }


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

    def area_of(address: str | None) -> str | None:
        return areas.name(same_place.get(address.strip(), address)) if address else None

    def route_of(segment: SkeletonSegment) -> tuple[str | None, str | None]:
        if segment.kind is not SegmentKind.MOVEMENT:
            return None, None
        origin, destination = segment.origin, segment.destination
        return (
            area_of(origin.address) if origin else None,
            area_of(destination.address) if destination else None,
        )

    segment_bundles = [
        ClueBundle(
            bundle_id="",
            start=segment.start,
            end=segment.end,
            segment=segment,
            base_event=build_base_event(
                segment, number, area_of(segment.address), route_of(segment)
            ),
        )
        for number, segment in enumerate(skeleton.segments, start=1)
    ]
    extra: list[ClueBundle] = []
    _embed_calendars(segment_bundles, calendars)

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
            hits = _overlapping(segment_bundles, clue.first_at, clue.first_at)
            if hits and clue.schedule_today is not False:
                hits[0].notifications.append(clue)
            else:
                day_notifications.append(clue)
            continue
        extra.append(_own(clue.first_at, clue.last_at or clue.first_at, conversations=[clue]))

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


def _embed_calendars(bundles: list[ClueBundle], calendars: list[CalendarClue]) -> None:
    """일정이 덮은 체류의 기본 event 를 일정 기본 event 로 바꾼다(사용자 결정 2026-10-30).

    배경 일정이 아니고 시각이 있는 일정이 체류 구간을 덮으면 — 구간 중 일정 밖 시간이 20분
    (`SHORT_STAY_MAX`) 이하 — 그 구간이 일정의 위치다. 일정 하나는 가장 오래 겹친 체류 하나에만
    넣는다. 같은 일정 시간 안의 다른 장소 체류는 자기 기본 event 그대로다(위치 뼈대는 살린다,
    사용자 결정). 긴 체류의 일부만 덮는 일정(9시간 사무실 안의 1시간 회의)은 넣지 않는다 — 그
    일정은 Timeline 이 체류 안으로 꺼낸다.
    """

    for calendar in calendars:
        if calendar.background or calendar.start is None or calendar.end is None:
            continue
        covered = [
            bundle
            for bundle in bundles
            if bundle.base_event is not None
            and bundle.base_event.calendar is None
            and bundle.segment is not None
            and bundle.segment.kind is SegmentKind.STAY
            and _outside(bundle.segment.start, bundle.segment.end, calendar) <= SHORT_STAY_MAX
        ]
        if not covered:
            continue
        bundle = max(
            covered,
            key=lambda b: min(b.segment.end, calendar.end) - max(b.segment.start, calendar.start),
        )
        bundle.base_event = with_calendar(bundle.base_event, calendar)


def _outside(start: datetime, end: datetime, calendar: CalendarClue) -> timedelta:
    """구간 중 일정 시간 밖의 길이."""

    before = max(timedelta(0), min(end, calendar.start) - start)
    after = max(timedelta(0), end - max(start, calendar.end))
    return before + after


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
    *,
    held_elsewhere: frozenset[str] | set[str] = frozenset(),
) -> list[list[SourceRef]]:
    """event 마다 근거 id 목록을 `sourceRefs` 로 펼친다.

    `held_elsewhere` 는 다른 event 가 이미 가진 rawId 다(Repair 가 event 하나를 고칠 때). 묶음을
    펼칠 때 그 사진·대화는 빼고, 단서 id 로 직접 적은 것만 담는다. 실제 Repair 가 회의 event 의 근거를
    `B5`·`C1` 로 다시 적자 B5 의 식사 사진까지 회의로 들어갔고, 사진 단일 귀속이 일정이 있는 회의에
    사진을 남겨 사진뿐이던 식사 event 가 지워졌다.

    기본 event id(`events_bases`)는 그 구간의 위치 기록이다. 묶음 id 는 그 구간과 거기 붙은 사진·일정·
    대화 전부이고 알림은 담지 않는다(#173). 다만 **다른 event 가 단서 id 로 직접 가져간 단서는 그 묶음을 펼칠 때 빠진다** — 묶음에 붙은 점심 사진을 다른 event 가 `P2` 로 가져갔으면
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
                    if clue.clue_id in evidence:
                        add(_raw_ids_of(clue), kind)
                        continue
                    if clue.clue_id in taken_directly:
                        continue
                    if kind != "calendar" and set(_raw_ids_of(clue)) <= set(held_elsewhere):
                        continue
                    add(_raw_ids_of(clue), kind)
        expanded.append(refs)
    return expanded


def misplaced_clues(
    clues: TimelineClues, start: datetime, end: datetime, refs: list[SourceRef]
) -> dict[str, str]:
    """event 의 시간·장소와 맞지 않는 사진·일정 근거. rawId → 단서 id(사용자 결정 2026-10-30·31).

    "시간과 장소 둘 다 봐야지 사진뿐만이 아니라 모든 근거들에 대해서". LLM 이 적은 근거를 그 자리에서
    본다 — 같은 곳에 두 번 간 날 12:49 점심 사진이 아침 체류(08:54~10:22)의 근거로 적혔고, Repair 가
    그 말을 믿고 아침 체류를 식사로 바꿨다.

    - 사진: 촬영 시각이 event 시간 앞뒤 20분(`SHORT_STAY_MAX`) 밖이거나, 위치를 아는데 event 가 가진
      체류 구간과 250m 넘게 떨어졌다. 체류 구간이 없으면 장소는 보지 않는다.
    - 일정: event 시간과 앞뒤 20분 안에서도 겹치지 않는다. 일정은 계획이라 시간이 조금 어긋날 수 있다.
    - 대화·알림은 보지 않는다. 약속 대화는 나중에 만난 event 의 근거이고, 알림의 받은 시각은 그 일이
      일어난 때가 아닐 수 있다.
    """

    held = {ref.raw_id for ref in refs}
    stays = [
        segment
        for segment in clues.skeleton.segments
        if segment.kind is SegmentKind.STAY and held & set(segment.raw_ids)
    ]
    low, high = start - SHORT_STAY_MAX, end + SHORT_STAY_MAX
    bad: dict[str, str] = {}
    for photo in clues.photos:
        if photo.raw_id not in held:
            continue
        if photo.taken_at is not None and not low <= photo.taken_at <= high:
            bad[photo.raw_id] = photo.clue_id
        elif stays and photo.latitude is not None and photo.longitude is not None and not any(
            _same_spot(photo, segment) for segment in stays
        ):
            bad[photo.raw_id] = photo.clue_id
    for calendar in clues.calendars:
        if calendar.raw_id not in held or calendar.start is None or calendar.end is None:
            continue
        if calendar.end < low or calendar.start > high:
            bad[calendar.raw_id] = calendar.clue_id
    return bad


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
        # 위치 구간에 붙은 사진은 어디인지를 묶음이 말한다. 사진의 주소·장소 후보까지 실으면 같은 곳에
        # 두 번 간 날 LLM 이 그 이름으로 다른 시간의 구간에 사진을 이었다(사용자 결정 2026-10-31).
        payload["photos"] = [
            _photo_dict(photo, clues, with_place=bundle.segment is None) for photo in bundle.photos
        ]
    if bundle.conversations:
        payload["conversations"] = [_notification_dict(clue) for clue in bundle.conversations]
    if bundle.notifications:
        payload["notifications"] = [_notification_dict(clue) for clue in bundle.notifications]
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


def _photo_dict(
    photo: PhotoClue, clues: TimelineClues, *, with_place: bool = True
) -> dict[str, object]:
    """사진 단서. `with_place` 가 거짓이면 주소·지명·장소 후보를 싣지 않는다 — 묶음이 장소를 말한다.

    장소 후보는 묶음의 `places` 에 이미 모여 있어 잃는 것이 없다.
    """

    payload: dict[str, object] = {"id": photo.clue_id}
    if photo.taken_at is not None:
        payload["takenAt"] = photo.taken_at.isoformat()
    for key, value in (
        ("activity", photo.activity),
        ("description", photo.description),
        ("address", clues.address(photo.address) if with_place else None),
        ("area", clues.area(photo.address) if with_place else None),
    ):
        if value:
            payload[key] = value
    if photo.places and with_place:
        payload["places"] = list(photo.places)
    if photo.uncertainty:
        payload["uncertainty"] = list(photo.uncertainty)
    return payload


def _notification_dict(clue: NotificationClue) -> dict[str, object]:
    """알림 단서. 받은 시각도 싣는다(#173) — 시각이 없자 Timeline 이 어느 event 의 것인지 알 수 없어
    결제·소식을 근거로 거의 쓰지 않았고, 위치 없는 날에는 결제·대화를 모두 버렸다."""

    payload: dict[str, object] = {"id": clue.clue_id}
    if clue.kind is not None:
        payload["kind"] = clue.kind.value
    if clue.app_names:
        payload["appNames"] = list(clue.app_names)
    for key, value in (("counterpart", clue.counterpart), ("summary", clue.summary)):
        if value:
            payload[key] = value
    if clue.schedule_today is not None:
        payload["scheduleToday"] = clue.schedule_today
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
