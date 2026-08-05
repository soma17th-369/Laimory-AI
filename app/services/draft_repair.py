"""Timeline draft 결정론적 repair.

LLM 은 draft 를 "대체로" 맞게 만든다. 이벤트 순서가 어긋나고, 같은 사건이 두 번
나오고, 지속시간이 0 이 되고, 식사가 세 시간이 된다. 이런 것들은 프롬프트로
확률적으로 고칠 문제가 아니라 **코드가 확정해야 할 문제**다.

main agent 그래프의 마지막 node 가 `repair_draft` 를 호출한다. Timeline Agent 는
LLM 병합과 파싱까지만 책임지고, 그 뒤의 확정은 전부 여기서 한다.

repair 순서와 이유:

   -1. `rawId 검증`     : 입력에 없는 참조를 제거하고 근거 없는 event 를 제외한다.
    0. `sourceType 정정` : LLM 이 붙인 근거 타입 라벨을 입력의 실제 타입으로 맞춘다.
    0.3 `수면 비노출`    : SLEEP/WAKE_UP event 를 빼고 수면 근거를 걷어 낸다.
    0.5 `캘린더 복원`    : timeline 에서 통째로 빠진 캘린더 일정을 event 로 되살린다.
    1. `duration repair` : 지속시간이 0 인 구간 event 를 근거 원본의 시간으로 복원한다.
    2. `근거 구간 스냅`  : 위치 근거만 가진 event 의 시간을 근거 원본 구간에 맞춘다.
    3. `MEAL repair`     : 과장된 식사 시간을 20~60분으로 되돌린다.
    5. `window 강제`     : 요청 시간 범위 밖 event 를 제거하고 경계를 클램프한다.
                           **조건 없이 항상 돈다.** 경계를 세우지 못하면 예외로 멈춘다.
    6. `장소 확정`       : placeLabel 을 근거의 장소명으로 채우고, 근거에 없는 주소를 지운다.
    7. `정렬`            : startTime → endTime → confidence(내림차순) → eventType → title.
    8. `체류 병합`       : 이동 없이 같은 장소에서 이어진 체류 event 를 하나로 합친다.
    9. `겹침 정리`       : 중복 event 를 병합하고, 모순되는 부분 겹침은 경고로 남긴다.
   10. `confidence 보강` : 캘린더 장소와 체류 장소가 일치하면 confidence 를 올린다.
   11. `clientEventId`   : 최종 정렬 결과에 1번부터 다시 부여하고 질문 참조를 보정한다.

`장소 확정`이 `겹침 정리`보다 앞에 있는 이유: 중복 판별이 placeLabel 을 쓰므로,
장소가 확정되기 전에 겹침을 보면 같은 곳의 두 event 를 다른 곳으로 오인한다.

`수면 경계 강제`(`sleep_guard.enforce_sleep_boundary`)는 이 파이프라인에서 빠졌다(#67).
수면 기록을 믿을 수 없다고 판단해 사용자 결과에서 수면을 뺐는데, 그 못 믿을 구간이
다른 event 를 계속 지우고 자르면 숨긴 정보가 뒤에서 결과를 만드는 셈이다. 기상 이전
event 를 막는 일은 `window 강제`가 대신한다. 서비스 파일 자체는 정확한 수면 데이터가
복구될 때를 위해 남겨 두었다.

`endTime < startTime` 은 `TimelineEventDraft` 스키마가 이미 거르므로(파싱 단계에서
해당 event 만 제외) 여기서 다시 다루지 않는다.

sourceRef 의 rawId 는 repair 진입 시 실제 입력 allowlist와 대조한다. LLM 이 지어낸
rawId 참조는 제거하고, 그 결과 유효한 근거가 하나도 남지 않은 event 는 제외한다.

다만 `sourceType` 라벨은 맨 앞에서 정정한다. 이것은 검증이 아니라 조회다. rawId 는
맞는데 타입만 틀린 근거(LLM 이 이동을 `STAY` 라고 적는 일이 실제로 일어난다)를 그대로
두면, 이후 단계가 그 근거를 영영 찾지 못한다.

겹침에 대하여: **포함 관계는 겹침이 아니다.** 긴 카페 체류(`REST`) 안에 짧은 식사
(`MEAL`)가 들어 있는 구조는 우리가 프롬프트로 요구한 정상적인 모양이다. 여기서
병합하는 것은 *같은 종류 + 같은 장소 + 시간이 겹치는* 중복 event 뿐이고, 서로 다른
장소를 동시에 가리키는 부분 겹침은 시간을 건드리지 않고 경고로만 남긴다.
"""

from datetime import datetime, tzinfo

from app.core.logging import get_logger, log_fields
from app.schemas import (
    EventSourceType,
    EventType,
    HealthMetric,
    SourceRef,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)
from app.services.calendar_guard import ensure_calendar_events
from app.services.calendar_location import reinforce_calendar_location
from app.services.duration_guard import verify_event_duration
from app.services.meal_guard import enforce_meal_duration
from app.services.narrative_guard import verify_narrative_length
from app.services.notification_guard import verify_notification_draft
from app.services.photo_guard import verify_photo_assignment
from app.services.place_resolver import resolve_places
from app.services.sleep_exclusion import apply_sleep_exclusion
from app.services.source_lookup import normalize_source_types, raw_id_of
from app.services.source_integrity import filter_draft_sources
from app.services.stay_merge import mergeable_stay_groups
from app.services.validator import (
    parse_datetime,
    renumber_events,
    resolve_timezone,
    resolve_window_bounds,
    validate_draft_to_window,
)

logger = get_logger(__name__)

#: 지속시간이 0 인 것이 정상인 event 종류.
#: 기상은 수면이 끝난 한 시점이고, 사진 순간은 셔터가 눌린 한 시점이다.
_INSTANT_EVENT_TYPES = frozenset({EventType.WAKE_UP, EventType.PHOTO_MOMENT})

#: 반드시 순간이어야 하는 event 종류. 구간으로 나오면 시작 시각으로 되돌린다.
_MUST_BE_INSTANT = frozenset({EventType.WAKE_UP})

#: 지속시간 복원에 쓸 수 있는 근거 종류. 실제 구간을 갖는 source 만 쓴다.
#: ACTIVITY(걸음 수)는 하루 전체를 덮는 집계라 복원 근거로 쓰면 event 가 하루가 된다.
_SPAN_SOURCE_TYPES = frozenset(
    {
        EventSourceType.STAY,
        EventSourceType.MOVEMENT,
        EventSourceType.CALENDAR,
        EventSourceType.SLEEP,
    }
)

#: 하루의 뼈대를 이루는 근거 종류. 이 근거만 가진 event 의 시간은 근거가 정한다.
_LOCATION_SOURCE_TYPES = frozenset({EventSourceType.STAY, EventSourceType.MOVEMENT})

#: 여정 근거. 이동을 근거로 댄 event 는 그 이동을 통째로 품어야 한다.
_MOVEMENT_SOURCE_TYPES = frozenset({EventSourceType.MOVEMENT})

#: 한 warning 에 담을 예시 개수.
_MAX_EXAMPLES = 3

def _add_warning(
    draft: TimelineDraft,
    severity: TimelineWarningSeverity,
    message: str,
    source_refs: list[SourceRef] | None = None,
) -> None:
    """repair 경고를 draft 에 붙인다. id 는 기존 warning 수에 이어 붙인다."""

    draft.warnings.append(
        TimelineWarning(
            warning_id=f"warning-repair-{len(draft.warnings) + 1:03d}",
            severity=severity,
            message=message,
            source_refs=source_refs or [],
        )
    )


def _examples(titles: list[str]) -> str:
    shown = ", ".join(titles[:_MAX_EXAMPLES])
    if len(titles) > _MAX_EXAMPLES:
        shown += f" 외 {len(titles) - _MAX_EXAMPLES}건"
    return shown


# --- 정렬 --------------------------------------------------------------------


def _sort_key(event: TimelineEventDraft):
    """확정 정렬 키.

    시작 시각이 하루의 순서를 만든다. 같은 시각에 시작하면 짧은 event 를 먼저 두어
    긴 배경 event 가 그 안의 짧은 사건을 감싸는 모양이 되게 한다. 그 다음은 확신이
    높은 event, 마지막으로 종류와 제목으로 완전히 결정론적인 순서를 만든다.
    """

    return (
        event.start_time,
        event.end_time,
        -event.confidence,
        event.event_type.value,
        event.title,
    )


def sort_events(draft: TimelineDraft) -> None:
    """event 를 시간 기준으로 확정 정렬한다(in-place). LLM 순서를 신뢰하지 않는다."""

    draft.events.sort(key=_sort_key)


# --- 지속시간 repair ----------------------------------------------------------


def _source_spans(
    request: TimelineDraftRequest, tz: tzinfo
) -> dict[tuple[EventSourceType, str], tuple[datetime, datetime]]:
    """구간을 갖는 입력 항목의 (sourceType, rawId) → (시작, 종료) 표."""

    spans: dict[tuple[EventSourceType, str], tuple[datetime, datetime]] = {}

    def collect(source_type: EventSourceType, items) -> None:
        for item in items:
            identifier = raw_id_of(item)
            if not identifier or not item.end_at:
                continue
            start = parse_datetime(item.start_at, tz)
            end = parse_datetime(item.end_at, tz)
            if start is None or end is None or end <= start:
                continue
            spans[(source_type, identifier)] = (start, end)

    collect(EventSourceType.STAY, request.stays)
    collect(EventSourceType.MOVEMENT, request.movements)
    collect(EventSourceType.CALENDAR, request.calendars)
    collect(
        EventSourceType.SLEEP,
        [item for item in request.healths if item.metric is HealthMetric.SLEEP],
    )
    return spans


def _referenced_span(
    event: TimelineEventDraft,
    spans: dict[tuple[EventSourceType, str], tuple[datetime, datetime]],
    source_types: frozenset[EventSourceType] = _SPAN_SOURCE_TYPES,
) -> tuple[datetime, datetime] | None:
    """event 가 근거로 삼은 원본 항목들이 덮는 시간 구간."""

    found = [
        spans[(ref.source_type, ref.raw_id)]
        for ref in event.source_refs
        if ref.source_type in source_types
        and (ref.source_type, ref.raw_id) in spans
    ]
    if not found:
        return None
    return min(start for start, _ in found), max(end for _, end in found)


def repair_durations(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """비정상 지속시간을 고친다(in-place).

    - 반드시 순간이어야 하는 event(기상)가 구간으로 나오면 시작 시각으로 되돌린다.
    - 구간이어야 할 event 의 지속시간이 0 이면 근거 원본의 시간으로 복원한다.
    - 복원할 근거가 없으면 시간은 그대로 두고 사실만 알린다. 없는 시간을 지어내지 않는다.

    식사(`MEAL`)의 지속시간은 `meal_guard` 가 전담하므로 여기서 건드리지 않는다.
    """

    tz = resolve_timezone(request.timezone)
    spans = _source_spans(request, tz)

    collapsed: list[str] = []
    restored: list[str] = []
    degenerate: list[str] = []

    for event in draft.events:
        if event.event_type in _MUST_BE_INSTANT:
            if event.end_time != event.start_time:
                event.end_time = event.start_time
                collapsed.append(event.title)
            continue

        if event.event_type is EventType.MEAL:
            continue  # meal_guard 담당
        if event.end_time > event.start_time:
            continue  # 정상 구간
        if event.event_type in _INSTANT_EVENT_TYPES:
            continue  # 사진 순간은 0 이 정상

        span = _referenced_span(event, spans)
        if span is None:
            degenerate.append(event.title)
            event.uncertainty.append("근거 원본에서 지속시간을 복원하지 못해 시작과 종료 시각이 같다.")
            continue
        event.start_time, event.end_time = span
        restored.append(event.title)

    if collapsed:
        _add_warning(
            draft,
            TimelineWarningSeverity.LOW,
            f"순간이어야 할 event {len(collapsed)}건을 시작 시각으로 되돌렸습니다: {_examples(collapsed)}",
        )
    if restored:
        _add_warning(
            draft,
            TimelineWarningSeverity.LOW,
            f"지속시간이 0인 event {len(restored)}건을 근거 원본 시간으로 복원했습니다: {_examples(restored)}",
        )
    if degenerate:
        _add_warning(
            draft,
            TimelineWarningSeverity.MEDIUM,
            f"지속시간이 0이고 근거에서 복원할 수 없는 event {len(degenerate)}건이 있습니다: {_examples(degenerate)}",
        )


# --- 근거 구간 스냅 -----------------------------------------------------------


def align_location_events(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """위치 근거만 가진 event 의 시간을 근거와 어긋나지 않게 맞춘다(in-place).

    LLM 은 체류·이동만 근거로 대 놓고 그 근거에 없는 시각을 적는다. 실제 출력에서
    한 체류 event 는 유일한 근거인 STAY 가 `22:33` 에 시작하는데 startTime 을
    `22:07` 로 당겨 놓았고, 왕복 산책 event 는 근거에 복귀 이동(`22:07` 종료)을 넣어
    놓고 endTime 은 `21:54`(편도)에서 끊었다. 둘 다 근거가 말하지 않는 시각이다.

    고치는 방향은 두 가지이고, 둘의 근거가 다르다.

        1. **근거 밖으로 나간 시간은 자른다.** 체류·이동이 말하지 않는 시각을 event 가
           주장할 수는 없다.
        2. **근거로 댄 이동은 통째로 품는다.** 이동은 하나의 여정이라 "일부만" 다녀올
           수 없다. 나갔다 돌아온 산책의 근거에 복귀 이동이 있으면 event 는 돌아온
           시각까지 이어진다.

    STAY 는 1번만 적용하고 2번은 적용하지 않는다. 체류 참조는 **시간이 아니라 장소를
    말하기 위한 인용일 수 있다.** `timeline.md` §1-5 가 긴 체류 안의 짧은 식사 event 에
    "장소를 말하기 위해 같은 STAY 를 함께 참조해도 된다"고 허용한다. 체류를 참조했다는
    이유로 event 를 체류 전체로 늘리면 그 구조가 무너진다.

    캘린더·사진·알림 근거가 섞인 event 는 아예 건드리지 않는다. 캘린더가 있으면 시간은
    캘린더가 정하고(§1-4), 사진·알림은 그 시점이 event 를 붙잡는 앵커다. `MEAL` 도
    제외한다(`meal_guard` 담당).
    """

    tz = resolve_timezone(request.timezone)
    spans = _source_spans(request, tz)
    aligned: list[str] = []

    for event in draft.events:
        if event.event_type is EventType.MEAL or event.event_type in _INSTANT_EVENT_TYPES:
            continue
        if not event.source_refs:
            continue
        if any(ref.source_type not in _LOCATION_SOURCE_TYPES for ref in event.source_refs):
            continue

        evidence = _referenced_span(event, spans, _LOCATION_SOURCE_TYPES)
        if evidence is None:
            continue  # 근거를 하나도 되짚을 수 없으면 손댈 기준이 없다

        start = max(event.start_time, evidence[0])
        end = min(event.end_time, evidence[1])
        if start >= end:
            # event 시간이 근거와 아예 겹치지 않는다. 어느 쪽이 틀렸는지 알 수 없으므로
            # 시간을 옮기지 않는다. 근거를 믿고 통째로 끌어오면 서로 무관한 event 들이
            # 같은 구간으로 몰려 하나로 뭉개진다.
            continue

        trip = _referenced_span(event, spans, _MOVEMENT_SOURCE_TYPES)
        if trip is not None:
            start, end = min(start, trip[0]), max(end, trip[1])

        if (start, end) == (event.start_time, event.end_time):
            continue
        event.start_time, event.end_time = start, end
        aligned.append(event.title)

    if aligned:
        _add_warning(
            draft,
            TimelineWarningSeverity.LOW,
            f"체류·이동 근거와 어긋난 event {len(aligned)}건의 시간을 근거에 맞췄습니다: "
            f"{_examples(aligned)}",
        )


# --- 겹침 정리 ----------------------------------------------------------------


def _place_key(event: TimelineEventDraft) -> tuple[str, str] | None:
    """중복 판별에 쓸 장소 열쇠. 장소를 모르면 ``None``.

    빈 장소 두 개를 같은 장소로 보면 안 된다(#67). 실제 로그에서 서로 다른 상대와
    주고받은 두 `SOCIAL` event 가 `같은 eventType + 빈 장소 + 시간 겹침` 만으로
    합쳐져, 다른 사람과의 대화가 한 사건이 됐다. 장소를 모른다는 것은 같은 곳이라는
    뜻이 아니라 **판단할 근거가 없다**는 뜻이다.
    """

    label = (event.place_label or "").strip()
    address = (event.address or "").strip()
    if not label and not address:
        return None
    return (label, address)


def _overlaps(left: TimelineEventDraft, right: TimelineEventDraft) -> bool:
    """시간이 겹치는가. 경계에서 맞닿기만 하는 것은 겹침이 아니다."""

    if left.start_time == right.start_time and left.end_time == right.end_time:
        return True  # 순간 event 두 개가 완전히 같은 시각인 경우까지 잡는다
    return left.start_time < right.end_time and right.start_time < left.end_time


def _contains(outer: TimelineEventDraft, inner: TimelineEventDraft) -> bool:
    return outer.start_time <= inner.start_time and inner.end_time <= outer.end_time


def _shares_raw_id(left: TimelineEventDraft, right: TimelineEventDraft) -> bool:
    """같은 근거 원본을 가리키는가. 같은 사건이라는 가장 강한 신호다."""

    return bool(
        {ref.raw_id for ref in left.source_refs}
        & {ref.raw_id for ref in right.source_refs}
    )


def _is_duplicate(left: TimelineEventDraft, right: TimelineEventDraft) -> bool:
    """같은 사건을 두 번 쓴 것인가.

    같은 종류 + 시간 겹침은 필요조건이지 충분조건이 아니다. 그 위에 **같은 장소**이거나
    **같은 근거 원본을 공유**해야 병합한다(#67). 장소를 둘 다 모르는 event 두 개는
    근거를 공유할 때만 같은 사건이다 — 애매하면 분리해 두고 Repair 의 판단에 맡긴다.
    잘못 합쳐 사실이 섞이는 쪽이 중복이 남는 쪽보다 나쁘다.
    """

    if left.event_type is not right.event_type or not _overlaps(left, right):
        return False

    place = _place_key(left)
    if place is not None and place == _place_key(right):
        return True
    return _shares_raw_id(left, right)


def _dedupe_refs(refs: list[SourceRef]) -> list[SourceRef]:
    seen: set[tuple[EventSourceType, str]] = set()
    unique: list[SourceRef] = []
    for ref in refs:
        key = (ref.source_type, ref.raw_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _absorb(target: TimelineEventDraft, other: TimelineEventDraft) -> None:
    """`other` 를 `target` 에 흡수한다. 서술은 확신이 높은 쪽을 남긴다."""

    if other.confidence > target.confidence:
        target.title = other.title
        target.description = other.description
        target.inference_level = other.inference_level

    target.start_time = min(target.start_time, other.start_time)
    target.end_time = max(target.end_time, other.end_time)
    target.confidence = max(target.confidence, other.confidence)
    target.address = target.address or other.address
    target.place_label = target.place_label or other.place_label
    target.tags = list(dict.fromkeys([*target.tags, *other.tags]))
    target.uncertainty = list(dict.fromkeys([*target.uncertainty, *other.uncertainty]))
    target.source_refs = _dedupe_refs([*target.source_refs, *other.source_refs])


def _remap_questions(draft: TimelineDraft, id_map: dict[str, str]) -> None:
    for question in draft.questions:
        question.related_event_ids = [
            id_map.get(event_id, event_id) for event_id in question.related_event_ids
        ]


def _is_pure_stay_event(event: TimelineEventDraft, group: frozenset[str]) -> bool:
    """근거가 이 묶음의 STAY 뿐인가. 그런 event 만 "여기 있었다" 자체를 말한다."""

    return bool(event.source_refs) and all(
        ref.source_type is EventSourceType.STAY and ref.raw_id in group
        for ref in event.source_refs
    )


def merge_stay_events(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    """이동 없이 같은 장소에서 이어진 체류 event 들을 하나로 합친다(in-place).

    어떤 STAY 들이 한 체류인지는 `stay_merge` 가 입력만 보고 판단한다. `_absorb` 가 시간
    구간을 근거 전체로 넓히므로, 흩어진 몇 분짜리 조각들이 하나의 연속 체류가 된다.

    합치는 것은 **순수한 체류 event** 뿐이다. 근거가 전부 그 묶음의 STAY 인 event 만이
    "여기 있었다"는 사실 자체를 말한다. 캘린더·사진·알림 근거가 섞여 있으면 그것은 체류
    조각이 아니라 그 체류 **안에서 일어난 사건**이고, STAY 참조는 시간이 아니라 장소를
    말하기 위한 인용이다(`align_location_events` 가 STAY 를 늘리지 않는 것과 같은 이유).

    이 구분을 놓치면 실제로 무너진다. 배경 캘린더 event(`09:00~23:00`)와 아이스크림 사진
    event 가 같은 체류를 참조했다는 이유로 빨려 들어가, `09:00~23:00 WORK 배스킨라빈스
    아이스크림을 사서 먹은 오후` 라는 event 가 나왔다.

    `resolve_overlaps` 로는 잡을 수 없다. 조각난 체류 event 들은 서로 **겹치지 않고**
    시간 공백을 사이에 두기 때문이다. 이 병합은 시간이 아니라 "사이에 이동이 없었다"는
    입력 사실에 근거한다. 호출 전에 event 가 시간순으로 정렬돼 있어야 한다.
    """

    tz = resolve_timezone(request.timezone)
    groups = mergeable_stay_groups(request, tz)
    if not groups:
        return

    id_map: dict[str, str] = {}
    merged_titles: list[str] = []
    absorbed: set[int] = set()  # 객체 identity. clientEventId 는 아직 중복일 수 있다.

    for group in groups:
        members = [
            event
            for event in draft.events
            if id(event) not in absorbed and _is_pure_stay_event(event, group)
        ]
        if len(members) < 2:
            continue

        target, *rest = members
        for event in rest:
            merged_titles.append(event.title)
            id_map[event.client_event_id] = target.client_event_id
            absorbed.add(id(event))
            _absorb(target, event)

    if not merged_titles:
        return

    draft.events = [event for event in draft.events if id(event) not in absorbed]
    _remap_questions(draft, id_map)
    _add_warning(
        draft,
        TimelineWarningSeverity.LOW,
        f"이동 없이 같은 장소에서 이어진 체류 event {len(merged_titles)}건을 "
        f"하나로 합쳤습니다: {_examples(merged_titles)}",
    )


def resolve_overlaps(draft: TimelineDraft) -> None:
    """중복 event 를 병합하고, 모순되는 부분 겹침은 경고로 남긴다(in-place).

    포함 관계(긴 체류 안의 짧은 식사 같은)는 정상이므로 손대지 않는다. 서로 다른
    장소를 같은 시간에 가리키는 부분 겹침은 사실 충돌이지만, 어느 쪽이 맞는지 코드가
    알 수 없으므로 시간을 자르지 않고 사용자에게 알린다.
    """

    kept: list[TimelineEventDraft] = []
    id_map: dict[str, str] = {}
    merged_titles: list[str] = []

    for event in draft.events:
        target = next((candidate for candidate in kept if _is_duplicate(candidate, event)), None)
        if target is None:
            kept.append(event)
            continue
        merged_titles.append(event.title)
        id_map[event.client_event_id] = target.client_event_id
        _absorb(target, event)

    draft.events = kept
    if id_map:
        _remap_questions(draft, id_map)
        _add_warning(
            draft,
            TimelineWarningSeverity.LOW,
            f"같은 사건을 가리키는 중복 event {len(merged_titles)}건을 병합했습니다: {_examples(merged_titles)}",
        )

    conflicts = [
        f"{left.title} ↔ {right.title}"
        for index, left in enumerate(kept)
        for right in kept[index + 1 :]
        if _overlaps(left, right)
        and not _contains(left, right)
        and not _contains(right, left)
    ]
    if conflicts:
        _add_warning(
            draft,
            TimelineWarningSeverity.LOW,
            f"시간이 서로 겹치는 event {len(conflicts)}쌍이 있습니다: {_examples(conflicts)}",
        )


# --- 질문 문장 다듬기 ---------------------------------------------------------


def _polish_questions(draft: TimelineDraft) -> None:
    """일반적인 검증 문구로 남은 질문을 사용자가 답할 수 있는 문장으로 바꾼다."""

    events_by_id = {event.client_event_id: event for event in draft.events}
    for question in draft.questions:
        if not _is_generic_question(question.question):
            continue
        related_event = next(
            (
                events_by_id[event_id]
                for event_id in question.related_event_ids
                if event_id in events_by_id
            ),
            None,
        )
        question.question = _event_specific_question(question.time_range, related_event)


def _is_generic_question(text: str) -> bool:
    normalized = text.strip().lower()
    generic = {
        "q",
        "확인 필요",
        "시간 확인 필요",
        "위치 확인 필요",
        "일정 확인 필요",
        "검증 필요",
    }
    return normalized in generic or len(normalized) <= 4


def _event_specific_question(time_range: dict, related_event) -> str:
    time_text = _korean_time_text(time_range.get("startTime"))
    if related_event is not None:
        return f"{time_text}쯤 {related_event.title} 활동이 맞나요?"
    return f"{time_text}쯤 있었던 활동이 맞나요?"


def _korean_time_text(value) -> str:
    if value is None:
        return "해당 시간"
    hour = value.hour
    minute = value.minute
    period = "오전" if hour < 12 else "오후"
    display_hour = hour if 1 <= hour <= 12 else abs(hour - 12)
    if display_hour == 0:
        display_hour = 12
    if minute:
        return f"{period} {display_hour}시 {minute:02d}분"
    return f"{period} {display_hour}시"


# --- 진입점 ------------------------------------------------------------------


def repair_draft(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    excluded_raw_ids: frozenset[str] = frozenset(),
) -> TimelineDraft:
    """LLM 이 만든 draft 를 코드로 확정한다(in-place, 같은 객체를 돌려준다).

    `excluded_raw_ids` 는 수면 비노출 정책이 걷어 낼 rawId 집합이다(#67). Repair 는
    매 확정 패스마다 현재 Event Agent 결과로 이 집합을 다시 계산해 넘긴다. 그래서
    Timeline 을 다시 돌리거나 Event Agent 를 다시 돌린 뒤에도 정책이 유지된다.
    """

    # 입력에 없는 rawId는 LLM 환각으로 본다. 잘못된 참조를 먼저 제거하고 유효한
    # 근거가 하나도 남지 않은 event는 이후 보정 단계로 넘기지 않는다.
    source_stats = filter_draft_sources(draft, request)
    if source_stats.changed:
        logger.debug(
            "입력에 없는 rawId 참조 정리",
            extra=log_fields(
                **source_stats.violation_log_fields(item_kind="TIMELINE_EVENT")
            ),
        )

    # 이후 모든 단계가 sourceRef 로 입력을 되짚으므로, LLM 이 붙인 sourceType 라벨을
    # 입력의 실제 타입으로 먼저 맞춘다. 라벨이 틀리면 근거를 영영 찾지 못한다.
    normalize_source_types(draft, request)

    # 수면 근거를 먼저 걷어 낸다. 뒤의 지속시간 복원·정렬·병합이 수면 rawId 를 근거로
    # 잡으면, 숨기기로 한 정보가 다른 event 의 시간을 만든다(#67).
    apply_sleep_exclusion(draft, excluded_raw_ids)

    # 되살린 캘린더 event 도 window·장소·정렬을 똑같이 거쳐야 하므로 여기서 채운다.
    ensure_calendar_events(draft, request, excluded_raw_ids)

    repair_durations(draft, request)
    align_location_events(draft, request)
    enforce_meal_duration(draft, request)

    # 조건 없이 항상 강제한다(#67). 경계를 세우지 못하면 `resolve_window_bounds` 가
    # 예외를 올린다 — 검증하지 못한 타임라인을 저장하는 것보다 멈추는 편이 낫다.
    validate_draft_to_window(draft, resolve_window_bounds(request))

    # 겹침 정리가 장소로 중복을 판별하므로, 그 전에 placeLabel 을 확정한다.
    resolve_places(draft, request)

    sort_events(draft)
    merge_stay_events(draft, request)
    resolve_overlaps(draft)

    # 사진 귀속 검사는 병합·겹침 정리 **뒤**여야 한다. 앞에 두면 곧 사라질 event 를
    # 기준으로 판정해, 병합으로 event 가 합쳐지면서 생긴 중복을 놓친다.
    verify_photo_assignment(draft, request)
    # Notification Agent 결과를 통과한 뒤에도 Timeline/Repair가 문장을 다시 조립하면서
    # 민감정보나 근거 없는 관계명을 만들 수 있어 최종 draft를 한 번 더 검사한다.
    verify_notification_draft(draft, request)

    reinforce_calendar_location(draft, request)
    # source 하나를 여러 event가 근거로 사용할 수 있다. 현재는 timeline_items에
    # event별 source 스냅샷을 저장하고, 향후 N:M 연결 테이블이 이 관계를 맡는다.

    # 모든 병합·문장 수정이 끝난 결과를 잰다(#61). 두 guard 다 Repair 반복에서
    # 자기 이전 warning 을 지우고 현재 draft 로 다시 계산하므로, Repair 가 문장을
    # 줄이거나 event 를 나눈 뒤 stale warning 이 남지 않는다.
    verify_narrative_length(draft)
    verify_event_duration(draft)

    # 병합으로 event 구성이 바뀌었을 수 있어 한 번 더 정렬한 뒤
    # 최종 id 를 부여한다. renumber_events 는 제거된 event 의 질문 참조도 버린다.
    sort_events(draft)
    renumber_events(draft)
    _polish_questions(draft)

    logger.debug(
        "draft repair 완료: events=%d, questions=%d, warnings=%d",
        len(draft.events),
        len(draft.questions),
        len(draft.warnings),
    )
    return draft
