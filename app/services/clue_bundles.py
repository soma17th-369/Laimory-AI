"""하루의 event 와 근거 (#169·#202, v4 세트).

**코드가 하루의 event 를 정한다**(#202). 위치 뼈대의 구간, 방문을 다 덮지 않는 일정, 어느 방문·일정에도
들지 않은 사진 장면이 각각 event 하나(기본 event, `base_events`)이고, 그 시간과 위치 기록·사진·일정 근거는
여기서 정한 값이다. Timeline·Repair 는 event id 마다 종류와 글만 쓴다. 예전에는 사진·일정을 걸친 구간 모두에
붙이고 어느 event 의 근거인지를 Timeline 이 골라, 같은 입력에서도 실행마다 사진이 다른 event 로 갔다.

묶음(`ClueBundle`) 하나가 기본 event 하나다. 대화만 예외로 event 가 없는 자기 묶음이다.

같은 입력이면 같은 결과다. 보는 것은 시간·좌표·일정의 장소 문자열뿐이고, 모델의 사진 설명이나 답의 순서는
보지 않는다.

**일정**(배경 일정이 아니고 시각이 있는 것)

- 체류·산책 방문을 덮으면 — 방문 중 일정 밖 시간이 20분(`SHORT_STAY_MAX`) 이하이고 장소가 충돌하지 않으면 —
  그 방문 event 가 일정 event 다. 시간은 방문이다(위치 기록이 경계를 정한다). 한 방문을 여러 일정이 덮거나
  한 일정이 여러 방문을 덮으면 하나씩만 짝짓는다. 장소가 맞는 쌍, 더 오래 겹친 쌍, 먼저 시작한 일정, 먼저
  시작한 방문 순이다(가장 오래 겹친 체류 하나, 사용자 결정 2026-10-30 에 장소 조건을 더했다).
- 그 밖의 일정은 자기 event 다. 시간은 일정이다. 긴 방문의 일부(09~18시 사무실 안의 14~15시 회의), 방문이
  없는 시간, 장소가 충돌하는 일정, 짝짓기에서 밀린 일정이 여기 든다. 그 시간과 겹치는 방문(장소 충돌이
  아닌 것)의 체류 기록을 장소 후보의 근거로 함께 갖는다.
- 배경 일정(종일·12시간 이상)은 자기 event 이고 방문을 합치거나 사진을 갖지 않는다. 안에 든 구간에는 배경
  일정이 있다는 표시만 단다. 캘린더 원본에서 종일로 표시된 일정은 하루 맨 앞에 둔다.
- 시각을 읽지 못한 일정은 event 가 되지 않는다(캘린더 복원도 시간이 없어 되살리지 못한다).

**사진**은 아래 순서로 처음 맞는 한 곳에만 든다(사용자 결정: 사진은 무조건 event 하나).

1. 촬영 시각이 체류·산책 방문 안이거나 경계 앞뒤 20분 안이고, 촬영 위치가 그 방문과 250m 이내인 방문.
   좌표를 모르면 시각이 방문 안일 때만이다. 산책은 움직이는 구간이라 위치를 보지 않는다. 후보가 여럿이면
   시각을 품는 방문, 가까운 방문, 먼저 시작한 방문 순이다.
2. 그 방문 안의 일정 event 가 촬영 시각을 품고 장소가 맞으면 그 일정 event. **장소를 모르는 일정은 방문
   사진을 가져가지 않는다** — 시간만 겹친 일정이 방문의 사진을 가져가지 않게 한다.
3. 방문이 없는 시간이면 촬영 시각을 품는 일정 event(배경 제외). 장소가 맞는 것, 짧은 것, 먼저 시작한 것 순이다.
4. 나머지는 같은 장면(20분·250m, `same_photo_scene`)끼리 사진 event 하나다. 이동 중, 빈 시간, 위치 없는 날의
   사진이 여기 든다. 가장 가까운 구간에 붙이지 않는다(#163).

시각을 읽지 못한 사진은 event 에 넣지 않고 사진 단일 귀속(#119)이 처리한다.

**장소가 맞음·충돌**: 일정의 장소명이 방문(사진)의 장소 후보와 같거나 일정 주소의 지명(구 + 동)이 같으면
맞는다. 둘 다 주소가 있는데 시·도나 시군구가 다르면 충돌이다. 그 밖은 모른다.

**대화**는 구간에 붙이지 않고 언제나 자기 묶음이다(#173, 사용자 결정 2026-10-31). "대화는 기존 이벤트와
연관이 크게 없다." Timeline·Repair 가 내용을 읽고 고를 때만 event 나 근거가 된다(#183). 대화 event 는 하루
3개까지이고 근거는 대화 알림뿐이다.

**그 밖의 알림**(결제·예약·소식·행동 결과)은 처음 받은 시각을 품는 구간 하나에 보여 준다(#173, 사용자 결정
2026-10-31). 근거는 아니다 — Timeline·Repair 가 내용으로 고른 event 의 근거가 된다. 구간 밖 시각의 알림과
다른 날 일정을 말하는 알림은 하루 목록으로 둔다. **단독 위치 기록**은 시간이 겹치는 묶음에 보여 준다.

event id(`E1`…)는 표시 순서다. 종일 일정이 먼저이고, 그다음 시작 시각, 같으면 짧은 것이 먼저다.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum

from app.schemas import (
    AgentWarning,
    EventSourceType,
    SourceRef,
    TimelineDraftRequest,
    TimelineEventDraft,
)
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.base_events import (
    AreaNamer,
    BaseEvent,
    area_namer,
    build_base_event,
    calendar_base_event,
    district_of,
    photo_base_event,
    structural_refs,
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
from app.services.place_text import normalize_place_text
from app.services.source_lookup import build_owner_index, raw_id_of


@dataclass
class ClueBundle:
    """기본 event 하나와 그 단서. 대화 묶음만 기본 event 가 없다."""

    bundle_id: str
    start: datetime | None
    end: datetime | None
    segment: SkeletonSegment | None = None
    #: 이 event 의 사진. 사진은 묶음 하나에만 있다(#202).
    photos: list[PhotoClue] = field(default_factory=list)
    #: 이 event 의 일정(0개나 1개). 구간을 덮은 일정이거나 이 event 자체인 일정이다.
    calendars: list[CalendarClue] = field(default_factory=list)
    conversations: list[NotificationClue] = field(default_factory=list)
    #: 이 구간의 시각에 받은, 대화가 아닌 알림(#173). 근거가 아니라 보여 주는 것이다.
    notifications: list[NotificationClue] = field(default_factory=list)
    isolated: list[LocationRecord] = field(default_factory=list)
    #: 이 구간을 덮는 배경 일정의 id. 배경 일정 자체는 자기 묶음에 있다.
    background_calendar_ids: list[str] = field(default_factory=list)
    #: 이 묶음의 기본 event(`E1`…). 대화 묶음에는 없다.
    base_event: BaseEvent | None = None
    #: 방문 안의 일정 event 가 함께 갖는 그 방문의 체류 기록.
    place_records: list[LocationRecord] = field(default_factory=list)
    #: 이 일정·사진 event 를 감싸는 방문의 묶음.
    host: "ClueBundle | None" = None

    @property
    def places(self) -> list[str]:
        """이 묶음의 장소 후보 풀. 구간의 이름이 앞이고, 함께 가진 체류·사진·일정의 이름이 뒤에 붙는다(#171)."""

        names = list(self.segment.places) if self.segment else []
        names.extend(
            name for record in self.place_records if record.spot for name in record.spot.places
        )
        names.extend(name for photo in self.photos for name in photo.places)
        names.extend(calendar.place for calendar in self.calendars if calendar.place)
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

    @property
    def conversation_ids(self) -> set[str]:
        """Notification 단서가 대화로 남긴 알림의 rawId."""

        return {
            raw_id
            for clue in self.notifications
            if clue.kind is NotificationClueKind.CONVERSATION
            for raw_id in clue.raw_ids
        }

    @property
    def base_events(self) -> list[BaseEvent]:
        """하루의 event. 표시 순서다."""

        return [bundle.base_event for bundle in self.bundles if bundle.base_event is not None]

    @property
    def structural_raw_ids(self) -> frozenset[str]:
        """코드가 귀속을 정하는 근거(위치 기록·사진·일정)의 rawId. 모델이 옮기지 않는다(#202)."""

        ids = set(self.skeleton.raw_ids())
        ids.update(photo.raw_id for photo in self.photos)
        ids.update(calendar.raw_id for calendar in self.calendars)
        return frozenset(ids)

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

    def photo_mismatch(self, event: TimelineEventDraft) -> tuple[list[str], list[str]] | None:
        """event 의 글이 적은 사진과 그 event 의 사진의 차이(#202). 모델이 쓴 글이 아니면 `None` 이다."""

        if event.photo_ids is None or not event.base_event_ids:
            return None
        bundle = self.base_bundle(event.base_event_ids[0])
        if bundle is None or bundle.base_event is None:
            return None
        return bundle.base_event.photo_mismatch(event.photo_ids)


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
    """뼈대와 단서로 하루의 event(기본 event)와 그 근거를 정한다(#202)."""

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
        ClueBundle(bundle_id="", start=segment.start, end=segment.end, segment=segment)
        for segment in skeleton.segments
    ]
    visits = [b for b in segment_bundles if b.segment.kind is not SegmentKind.MOVEMENT]
    timed = [c for c in calendars if c.start is not None and c.end is not None]
    order = {calendar.clue_id: index for index, calendar in enumerate(calendars)}

    linked = _link_calendars(visits, [c for c in timed if not c.background], order)
    for bundle, calendar in linked:
        bundle.calendars = [calendar]
    linked_ids = {calendar.clue_id for _, calendar in linked}

    calendar_bundles: list[ClueBundle] = []
    for calendar in timed:
        if calendar.clue_id in linked_ids:
            continue
        if calendar.background:
            for bundle in _overlapping(segment_bundles, calendar.start, calendar.end):
                bundle.background_calendar_ids.append(calendar.clue_id)
            calendar_bundles.append(_own(calendar.start, calendar.end, calendars=[calendar]))
            continue
        hosts = [
            b
            for b in visits
            if _overlap(b.segment.start, b.segment.end, calendar.start, calendar.end) > timedelta(0)
            and _relation(calendar, b.segment.places, b.segment.address) is not _Relation.CONFLICT
        ]
        own = _own(calendar.start, calendar.end, calendars=[calendar])
        own.host = min(
            hosts,
            key=lambda b: (
                -_overlap(b.segment.start, b.segment.end, calendar.start, calendar.end),
                b.segment.start,
            ),
            default=None,
        )
        own.place_records = [
            record
            for b in hosts
            if b.segment.kind is SegmentKind.STAY
            for record in b.segment.records
            if _touches(record.start, record.end, calendar.start, calendar.end)
        ]
        calendar_bundles.append(own)

    photo_bundles: list[ClueBundle] = []
    timed_photos = [p for p in photos if p.taken_at is not None]
    for photo in sorted(timed_photos, key=lambda p: p.taken_at):
        _photo_owner(photo, visits, calendar_bundles, photo_bundles, order).photos.append(photo)

    day_notifications: list[NotificationClue] = []
    conversation_bundles: list[ClueBundle] = []
    for clue in notifications:
        if clue.kind is not NotificationClueKind.CONVERSATION:
            hits = _overlapping(segment_bundles, clue.first_at, clue.first_at)
            if hits and clue.schedule_today is not False:
                hits[0].notifications.append(clue)
            else:
                day_notifications.append(clue)
            continue
        conversation_bundles.append(
            _own(clue.first_at, clue.last_at or clue.first_at, conversations=[clue])
        )

    for bundle in segment_bundles:
        base = build_base_event(bundle.segment, 0, area_of(bundle.segment.address), route_of(bundle.segment))
        if bundle.calendars:
            base = with_calendar(base, bundle.calendars[0])
        bundle.base_event = replace(base, photos=tuple(bundle.photos))
    for bundle in calendar_bundles:
        bundle.base_event = replace(
            calendar_base_event(bundle.calendars[0], place_records=bundle.place_records),
            photos=tuple(bundle.photos),
        )
    for bundle in photo_bundles:
        bundle.base_event = photo_base_event(bundle.photos)
        bundle.start, bundle.end = bundle.base_event.start, bundle.base_event.end

    events = sorted([*segment_bundles, *calendar_bundles, *photo_bundles], key=_display_key)
    for number, bundle in enumerate(events, start=1):
        bundle.base_event = replace(bundle.base_event, base_id=f"E{number}")
    for bundle in events:
        if bundle.host is not None:
            bundle.base_event = replace(bundle.base_event, inside=bundle.host.base_event.base_id)

    bundles = [*events, *sorted(conversation_bundles, key=lambda b: (b.start is None, b.start or datetime.min))]
    bundles.sort(key=lambda b: (b.base_event is None or not b.base_event.all_day, _start_key(b)))
    for number, bundle in enumerate(bundles, start=1):
        bundle.bundle_id = f"B{number}"

    loose: list[LocationRecord] = []
    for record in skeleton.isolated:
        hits = _overlapping(segment_bundles, record.start, record.end) or [
            b
            for b in _overlapping(events, record.start, record.end)
            if not b.calendars or not b.calendars[0].background
        ]
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


def display_key(base: BaseEvent) -> tuple:
    """event 의 표시 순서. 종일 일정이 먼저이고, 그다음 시작 시각, 같으면 짧은 것이 먼저다(#202)."""

    return (not base.all_day, base.start, base.end, _KIND_ORDER[base.kind.value])


#: 같은 시간의 기본 event 를 늘어놓는 순서. 위치 구간이 먼저다.
_KIND_ORDER = {"STAY": 0, "WALK": 1, "MOVEMENT": 2, "CALENDAR": 3, "PHOTO": 4}


def _display_key(bundle: ClueBundle) -> tuple:
    calendar = bundle.calendars[0] if bundle.calendars else None
    segment = bundle.segment
    all_day = segment is None and calendar is not None and calendar.all_day
    kind = segment.kind.value if segment else ("CALENDAR" if calendar else "PHOTO")
    first = (
        segment.raw_ids[0] if segment and segment.raw_ids
        else calendar.raw_id if calendar
        else bundle.photos[0].raw_id
    )
    return (not all_day, bundle.start, bundle.end, _KIND_ORDER[kind], first)


def _start_key(bundle: ClueBundle) -> tuple:
    return (bundle.start is None, bundle.start or datetime.min)


class _Relation(Enum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


def _relation(calendar: CalendarClue, names, address: str | None) -> _Relation:
    """일정의 장소와 방문·사진의 장소가 맞는가."""

    if calendar.place and normalize_place_text(calendar.place) in {
        normalize_place_text(name) for name in names if name
    }:
        return _Relation.MATCH
    if not calendar.address or not address:
        return _Relation.UNKNOWN
    planned, there = AreaNamer().name(calendar.address), AreaNamer().name(address)
    if planned and planned == there:
        return _Relation.MATCH
    left, right = district_of(calendar.address), district_of(address)
    if left and right and (
        left[1] != right[1] or (left[0] is not None and right[0] is not None and left[0] != right[0])
    ):
        return _Relation.CONFLICT
    return _Relation.UNKNOWN


def _link_calendars(
    visits: list[ClueBundle], calendars: list[CalendarClue], order: dict[str, int]
) -> list[tuple[ClueBundle, CalendarClue]]:
    """방문을 덮는 일정과 그 방문을 하나씩 짝짓는다(사용자 결정 2026-10-30, #202)."""

    pairs = []
    for calendar in calendars:
        for index, bundle in enumerate(visits):
            segment = bundle.segment
            overlap = _overlap(segment.start, segment.end, calendar.start, calendar.end)
            if overlap <= timedelta(0) or _outside(segment.start, segment.end, calendar) > SHORT_STAY_MAX:
                continue
            relation = _relation(calendar, segment.places, segment.address)
            if relation is _Relation.CONFLICT:
                continue
            key = (
                relation is not _Relation.MATCH,
                -overlap,
                calendar.start,
                order[calendar.clue_id],
                segment.start,
                index,
            )
            pairs.append((key, bundle, calendar))
    linked: list[tuple[ClueBundle, CalendarClue]] = []
    taken_visits: set[int] = set()
    taken_calendars: set[str] = set()
    for _, bundle, calendar in sorted(pairs, key=lambda pair: pair[0]):
        if id(bundle) in taken_visits or calendar.clue_id in taken_calendars:
            continue
        linked.append((bundle, calendar))
        taken_visits.add(id(bundle))
        taken_calendars.add(calendar.clue_id)
    return linked


def _photo_owner(
    photo: PhotoClue,
    visits: list[ClueBundle],
    calendar_bundles: list[ClueBundle],
    photo_bundles: list[ClueBundle],
    order: dict[str, int],
) -> ClueBundle:
    """사진이 들어갈 묶음 하나(모듈 docstring 의 1~4)."""

    taken = photo.taken_at
    visit = _photo_visit(photo, visits)
    if visit is not None:
        inner = [
            b
            for b in calendar_bundles
            if b.host is visit
            and b.calendars[0].start <= taken <= b.calendars[0].end
            and _relation(b.calendars[0], photo.places, photo.address) is _Relation.MATCH
        ]
        return min(inner, key=lambda b: _calendar_rank(b.calendars[0], photo, order), default=visit)

    free = [
        b
        for b in calendar_bundles
        if b.host is None
        and not b.calendars[0].background
        and b.calendars[0].start <= taken <= b.calendars[0].end
    ]
    if free:
        return min(free, key=lambda b: _calendar_rank(b.calendars[0], photo, order))

    scene = photo_bundles[-1] if photo_bundles else None
    if scene is not None and same_photo_scene(
        scene.photos[-1].taken_at,
        (scene.photos[-1].latitude, scene.photos[-1].longitude),
        taken,
        (photo.latitude, photo.longitude),
    ):
        return scene
    scene = _own(taken, taken)
    photo_bundles.append(scene)
    return scene


def _calendar_rank(calendar: CalendarClue, photo: PhotoClue, order: dict[str, int]) -> tuple:
    return (
        _relation(calendar, photo.places, photo.address) is not _Relation.MATCH,
        calendar.end - calendar.start,
        calendar.start,
        order[calendar.clue_id],
    )


def _photo_visit(photo: PhotoClue, visits: list[ClueBundle]) -> ClueBundle | None:
    """사진이 찍힌 방문. 시각과 위치가 모두 맞아야 한다."""

    taken = photo.taken_at
    point = (photo.latitude, photo.longitude) if photo.latitude is not None and photo.longitude is not None else None
    found = []
    for index, bundle in enumerate(visits):
        segment = bundle.segment
        inside = segment.start <= taken <= segment.end
        if segment.kind is SegmentKind.WALK or point is None:
            if inside:
                found.append(((0, 0.0, segment.start, index), bundle))
            continue
        if not segment.start - SHORT_STAY_MAX <= taken <= segment.end + SHORT_STAY_MAX:
            continue
        distance = _distance_to(point, segment)
        if distance is None:
            if inside:
                found.append(((0, 0.0, segment.start, index), bundle))
            continue
        if distance <= SAME_PLACE_MAX_METERS:
            found.append(((0 if inside else 1, distance, segment.start, index), bundle))
    return min(found, key=lambda item: item[0])[1] if found else None


def _distance_to(point: tuple[float, float], segment: SkeletonSegment) -> float | None:
    """구간의 지점들과 가장 가까운 거리. 체류는 구간 안 기록의 지점도 본다."""

    points = [segment.spot.point] if segment.spot and segment.spot.point else []
    if segment.kind is SegmentKind.STAY:
        points.extend(r.spot.point for r in segment.records if r.spot and r.spot.point)
    distances = [d for p in points if (d := distance_between(point, p)) is not None]
    return min(distances) if distances else None


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


def _overlap(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> timedelta:
    return max(timedelta(0), min(end, other_end) - max(start, other_start))


def _touches(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    """두 시간이 겹치는가. 한 점짜리 일정은 그 시각을 품는 기록과 겹친다."""

    if other_start == other_end:
        return start <= other_start <= end
    return start < other_end and other_start < end


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


# --- 근거 id 펼치기 -----------------------------------------------------------------

#: 단서 종류마다 `sourceRefs.reason` 에 코드가 적는 말.
_REASONS = {
    "photo": "사진",
    "calendar": "일정",
    "conversation": "대화 알림",
    "notification": "알림",
}


def expand_evidence(
    clues: TimelineClues, identifiers: list[str], request: TimelineDraftRequest
) -> list[SourceRef]:
    """근거 id(event `E3`, 단서 `P2`·`C1`·`N5`)를 `sourceRefs` 로 펼친다. 모르는 id 는 버린다.

    event id 는 그 기본 event 의 위치 기록·일정·사진이다. warning 이 가리키는 근거를 rawId 로 바꿀 때 쓴다.
    같은 rawId 는 한 번만 담는다.
    """

    by_clue_id = _clue_index(clues)
    owners = build_owner_index(request)
    refs: list[SourceRef] = []
    seen: set[str] = set()

    def add(raw_id: str, reason: str) -> None:
        source_type = _source_type(owners, raw_id)
        if raw_id in seen or source_type is None:
            return
        seen.add(raw_id)
        refs.append(SourceRef(source_type=source_type, raw_id=raw_id, reason=reason))

    for identifier in identifiers:
        identifier = str(identifier).strip()
        if identifier in by_clue_id:
            kind, raw_ids = by_clue_id[identifier]
            for raw_id in raw_ids:
                add(raw_id, _REASONS[kind])
        elif (bundle := clues.base_bundle(identifier)) is not None:
            for ref in structural_refs(bundle.base_event):
                add(ref.raw_id, ref.reason or "")
    return refs


def notification_evidence(
    clues: TimelineClues, identifiers: list[str], request: TimelineDraftRequest
) -> tuple[list[SourceRef], list[str]]:
    """모델이 고르는 근거(알림·대화)만 펼친다(#202). 펼치지 않은 id 를 함께 돌려준다.

    위치 기록·사진·일정은 코드가 정한 근거라 모델이 옮기지 않는다. 알림 rawId 를 직접 적어도 받는다.
    """

    by_clue_id = _clue_index(clues)
    notification_ids = {raw_id_of(item) for item in request.notifications}
    refs: list[SourceRef] = []
    seen: set[str] = set()
    ignored: list[str] = []
    for identifier in identifiers:
        identifier = str(identifier).strip()
        found = by_clue_id.get(identifier)
        if found is not None and found[0] in ("conversation", "notification"):
            pairs = [(raw_id, _REASONS[found[0]]) for raw_id in found[1]]
        elif identifier in notification_ids:
            pairs = [(identifier, _REASONS["notification"])]
        else:
            ignored.append(identifier)
            continue
        for raw_id, reason in pairs:
            if raw_id not in seen:
                seen.add(raw_id)
                refs.append(
                    SourceRef(source_type=EventSourceType.NOTIFICATION, raw_id=raw_id, reason=reason)
                )
    return refs, ignored


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
    """Timeline·Repair v4 가 읽는 `[하루]`·`[event]`·`[대화]`·`[빈 시간]`·`[알림]`."""

    skeleton = clues.skeleton
    day: dict[str, object] = {"hasLocation": skeleton.has_location}
    if skeleton.first_observed_at:
        day["firstObservedAt"] = skeleton.first_observed_at.isoformat()
    if skeleton.last_observed_at:
        day["lastObservedAt"] = skeleton.last_observed_at.isoformat()
    if skeleton.last_spot and (label := skeleton.last_spot.label):
        day["lastPlace"] = label
    contains: dict[str, list[str]] = {}
    for base in clues.base_events:
        if base.inside:
            contains.setdefault(base.inside, []).append(base.base_id)
    return {
        "day": day,
        "events": [
            _event_dict(bundle, clues, contains.get(bundle.base_event.base_id, []))
            for bundle in clues.bundles
            if bundle.base_event is not None
        ],
        "conversations": [
            _notification_dict(clue) for bundle in clues.bundles for clue in bundle.conversations
        ],
        "emptyTimes": [_empty_dict(empty, clues) for empty in skeleton.empty_times],
        "notifications": [_notification_dict(clue) for clue in clues.day_notifications],
    }


def _event_dict(bundle: ClueBundle, clues: TimelineClues, contains: list[str]) -> dict[str, object]:
    base = bundle.base_event
    payload: dict[str, object] = {
        "id": base.base_id,
        "kind": base.kind.value,
        "startAt": base.start.isoformat(),
        "endAt": base.end.isoformat(),
    }
    if bundle.photos:
        # 시간 바로 다음에 싣는다(#202). 긴 위치 기록 뒤 맨 끝에 두었을 때 Timeline 이 사진을 다음 event 의
        # 것으로 읽었다. 위치 구간의 사진은 어디인지를 구간이 말한다 — 사진의 주소·장소 후보까지 실으면 같은
        # 곳에 두 번 간 날 LLM 이 그 이름으로 다른 시간의 구간에 사진을 이었다(사용자 결정 2026-10-31).
        payload["photos"] = [
            _photo_dict(photo, clues, with_place=bundle.segment is None) for photo in bundle.photos
        ]
    if base.fixed_type is not None:
        payload["eventType"] = base.fixed_type.value
    if base.inside:
        payload["inside"] = base.inside
    if contains:
        payload["contains"] = contains
    payload["frame"] = base.as_prompt_dict()
    if bundle.segment is not None:
        payload["location"] = _segment_dict(bundle.segment, clues)
    if bundle.places:
        payload["places"] = bundle.places
    if bundle.background_calendar_ids:
        payload["backgroundCalendars"] = list(bundle.background_calendar_ids)
    if bundle.calendars:
        [calendar] = bundle.calendars
        payload["calendar"] = {
            **calendar.as_prompt_dict(),
            **({"area": a} if (a := clues.area(calendar.address)) else {}),
        }
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
    if clue.kind is NotificationClueKind.CONVERSATION:
        payload["messageCount"] = len(clue.raw_ids)
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
