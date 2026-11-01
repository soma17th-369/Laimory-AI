"""Timeline draft 결정론적 repair.

LLM 은 draft 를 "대체로" 맞게 만든다. 이벤트 순서가 어긋나고, 같은 사건이 두 번
나오고, 지속시간이 0 이 되고, 식사가 세 시간이 된다. 이런 것들은 프롬프트로
확률적으로 고칠 문제가 아니라 **코드가 확정해야 할 문제**다.

main agent 그래프의 마지막 node 가 `repair_draft` 를 호출한다. Timeline Agent 는
LLM 병합과 파싱까지만 책임지고, 그 뒤의 확정은 전부 여기서 한다.

repair 순서와 이유:

   -1. `rawId 검증`     : 입력에 없는 참조를 제거하고 근거 없는 event 를 제외한다.
    0. `sourceType 정정` : LLM 이 붙인 근거 타입 라벨을 입력의 실제 타입으로 맞춘다.
    0.5 `캘린더 복원`    : timeline 에서 통째로 빠진 캘린더 일정을 event 로 되살린다.
    1. `duration repair` : WAKE_UP 은 순간으로 되돌리고, 지속시간이 0 인 구간 event 는
                           근거 원본의 시간으로 복원한다.
    2. `근거 구간 스냅`  : 위치 근거만 가진 event 의 시간을 근거 원본 구간에 맞춘다.
    3. `MEAL repair`     : 과장된 식사 시간을 20~60분으로 되돌린다.
    4. `수면 경계 강제`  : 기상 이전 event 를 제거하고, 수면에 걸친 event 를 잘라 낸다.
    5. `window 강제`     : 요청 시간 범위 밖 event 를 제거하고 경계를 클램프한다.
    5.5 `위치 근거 연결` : (v3) 일정·사진 event 에 같은 시간의 체류를 장소 근거로 붙인다(#138).
    6. `장소 확정`       : place 를 근거의 장소명으로 채우고, 근거에 없는 주소를 지운다.
    7. `정렬`            : startTime → endTime → confidence(내림차순) → eventType → title.
    8. `체류 병합`       : 이동 없이 같은 장소에서 이어진 체류 event 를 하나로 합친다.
    9. `겹침 정리`       : 중복 event 를 병합하고, 모순되는 부분 겹침은 경고로 남긴다.
   10. `사진 단일 귀속`  : 입력의 모든 사진이 정확히 한 event 에만 있게 만든다(#119).
   11. `confidence 보강` : 캘린더 장소와 체류 장소가 일치하면 confidence 를 올린다.
   11.5 (v3) 수면과 겹친 event 제거, 대화 개수 제한, event 개수 정리(#163), 문장의 입력
        주소 원문 정리(#138).
   12. `검사`            : 고치지 않고 찾기만 한다. 사진·알림 안전성, 문장 길이, 타입별
                           지속시간, event 개수, 이동 사이 장시간 체류, (v3) 일정·사진과
                           겹치는 위치만 있는 event, 제목의 체류·시각과 문장의 주소 모양.
   13. `clientEventId`   : 새 event 에만 번호를 준다. Repair 가 돌 때는 이미 준 번호를
                           유지하고 지운 번호도 다시 쓰지 않는다(#144). 번호는 여기서만 준다.

1~11 은 draft 를 고치고 12 는 고치지 않는다. **무엇을 고칠지가 규칙으로 정해져 있으면
코드가 고치고, 어디서 끊고 무엇을 남길지가 의미 판단이면 찾아서 Repair 에 넘긴다.**
이동 사이의 장시간 체류를 나누는 것이 뒤쪽이다 — 나눈 조각마다 무엇을 했는지 다시 써야
한다. 대화 event 를 3개로 줄이는 것과 10개를 넘을 때 알림만 근거인 event 부터 지우는 것은
기준이 알림 수 하나라 코드가 한다.

단계마다 직전·직후의 event 를 비교해 무엇이 바뀌었는지 적는다(`confirm_report`). Repair
는 그 기록을 받아 코드가 이미 본 것을 다시 검증하지 않고, 보정으로 어색해진 내용과
문장을 다듬는다.

`장소 확정`이 `겹침 정리`보다 앞에 있는 이유: 중복 판별이 place 를 쓰므로,
장소가 확정되기 전에 겹침을 보면 같은 곳의 두 event 를 다른 곳으로 오인한다.

`수면 경계 강제`가 `겹침 정리`보다 앞에 있는 이유: 수면 중 event 를 먼저 걷어 내지
않으면 `수면 ↔ 새벽에 시작된 아침 event` 가 "모순되는 겹침" 경고로 남는다. 그 겹침은
사실 충돌이 아니라 잘못된 event 하나가 만든 것이다.

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
장소를 동시에 가리키는 부분 겹침은 시간을 건드리지 않고 경고로만 남긴다. 장소가 비어
있는 event 끼리는 같은 근거를 함께 가질 때만 중복으로 본다(#163).

일정·사진 event 와 같은 방문을 그린 체류 카드(#138)도 코드가 합치지 않는다. 같은 방문인지
더 긴 다른 시간(근무 체류 안의 회의)인지는 의미 판단이라, v3 에서는 찾아서 Repair 에 넘기고
Repair 가 `absorb_location_event` 로 흡수한다. 일정·사진 event 는 어떤 단계도 지우지 않는다.
"""

from collections.abc import Callable, Sequence
from datetime import datetime, tzinfo
from functools import partial

from app.core.logging import get_logger, log_fields
from app.schemas import (
    AiEventCandidate,
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
from app.schemas.clues import NotificationClueKind
from app.services.calendar_guard import ensure_calendar_events
from app.services.calendar_location import reinforce_calendar_location
from app.services.clue_bundles import TimelineClues
from app.services.confirm_report import ConfirmReport
from app.services.conversation_guard import enforce_conversation_limit
from app.services.duration_guard import verify_event_duration
from app.services.event_count_guard import (
    LEGACY_MAX_EVENT_COUNT,
    MAX_EVENT_COUNT,
    enforce_event_count,
    verify_event_count,
)
from app.services.location_link import link_location_evidence
from app.services.location_only_overlap import find_location_only_overlaps
from app.services.location_skeleton import LocationSkeleton
from app.services.meal_guard import enforce_meal_duration, find_unsupported_meals
from app.services.movement_stay_guard import verify_movement_stay_boundary
from app.services.narrative_guard import verify_narrative_length
from app.services.narrative_place_guard import (
    find_narrative_labels,
    replace_input_addresses,
)
from app.services.notification_guard import verify_notification_draft
from app.services.photo_guard import (
    enforce_photo_assignment,
    enforce_photo_scenes,
    verify_photo_assignment,
)
from app.services.place_resolver import resolve_places
from app.services.place_slots import (
    confirm_place_slots,
    fill_place_slots,
    keep_place_names_out,
    place_names_in_title,
)
from app.services.skeleton_guard import (
    remove_calendar_events_without_calendar,
    remove_stays_without_location,
    NEEDS_WRITING,
    conversations_on_other_events,
    ensure_notification_events,
    ensure_skeleton_segments,
    hand_restored_events_to_repair,
    keep_calendar_base_events,
    keep_stay_word_out,
    remove_events_outside_evidence,
    remove_notification_events,
    remove_unrecorded_movements,
    retire_unknown_events,
    settle_event_evidence,
    separate_movement_segments,
    spares_from_sleep,
)
from app.services.sleep_guard import (
    enforce_sleep_boundary,
    remove_events_overlapping_sleep,
)
from app.services.source_lookup import normalize_source_types, raw_id_of
from app.services.source_integrity import filter_draft_sources
from app.services.stay_merge import mergeable_stay_groups
from app.services.text_suggestions import suggest_text_fixes
from app.services.validator import (
    assign_event_ids,
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
    request: TimelineDraftRequest, tz: tzinfo, skeleton: LocationSkeleton | None = None
) -> dict[tuple[EventSourceType, str], tuple[datetime, datetime]]:
    """구간을 갖는 입력 항목의 (sourceType, rawId) → (시작, 종료) 표.

    `skeleton` 을 주면(v4) 체류·이동은 원래 시각이 아니라 뼈대 구간 안의 시간이다
    (`LocationSkeleton.record_spans`). 원래 시각을 쓰면 뼈대가 정리한 이동 시간이 원본으로
    되늘어나 앞 체류와 겹쳤다.
    """

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
    if skeleton is not None:
        for raw_id, span in skeleton.record_spans().items():
            for source_type in (EventSourceType.STAY, EventSourceType.MOVEMENT):
                if (source_type, raw_id) in spans:
                    spans[(source_type, raw_id)] = span
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


def repair_durations(
    draft: TimelineDraft, request: TimelineDraftRequest, *, skeleton: LocationSkeleton | None = None
) -> None:
    """비정상 지속시간을 고친다(in-place).

    - 반드시 순간이어야 하는 event(기상)가 구간으로 나오면 시작 시각으로 되돌린다.
    - 구간이어야 할 event 의 지속시간이 0 이면 근거 원본의 시간으로 복원한다.
    - 복원할 근거가 없으면 시간은 그대로 두고 사실만 알린다. 없는 시간을 지어내지 않는다.

    식사(`MEAL`)의 지속시간은 `meal_guard` 가 전담하므로 여기서 건드리지 않는다.
    """

    tz = resolve_timezone(request.timezone)
    spans = _source_spans(request, tz, skeleton)

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


def align_location_events(
    draft: TimelineDraft, request: TimelineDraftRequest, *, skeleton: LocationSkeleton | None = None
) -> None:
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
    spans = _source_spans(request, tz, skeleton)
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


def _place_key(event: TimelineEventDraft) -> tuple[str, str]:
    return (event.place or "", event.address or "")


def _overlaps(left: TimelineEventDraft, right: TimelineEventDraft) -> bool:
    """시간이 겹치는가. 경계에서 맞닿기만 하는 것은 겹침이 아니다."""

    if left.start_time == right.start_time and left.end_time == right.end_time:
        return True  # 순간 event 두 개가 완전히 같은 시각인 경우까지 잡는다
    return left.start_time < right.end_time and right.start_time < left.end_time


def _contains(outer: TimelineEventDraft, inner: TimelineEventDraft) -> bool:
    return outer.start_time <= inner.start_time and inner.end_time <= outer.end_time


def _is_duplicate(left: TimelineEventDraft, right: TimelineEventDraft) -> bool:
    """같은 종류 + 같은 장소 + 시간이 겹치면 같은 사건을 두 번 쓴 것으로 본다.

    장소가 비어 있으면 장소로는 같은 곳인지 알 수 없다. 그때는 같은 근거(rawId)를 하나
    이상 함께 가질 때만 같은 사건으로 본다(#163). 빈 키를 같은 장소로 치면 근거가 알림뿐인
    event 들이 시간만 겹쳐도 합쳐진다 — 서로 다른 상대와의 대화 셋이 11시간짜리 대화
    하나가 됐다.
    """

    if left.event_type is not right.event_type or not _overlaps(left, right):
        return False
    place = _place_key(left)
    if place != _place_key(right):
        return False
    if any(place):
        return True
    return not {ref.raw_id for ref in left.source_refs}.isdisjoint(
        ref.raw_id for ref in right.source_refs
    )


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
    target.place = target.place or other.place
    target.tags = list(dict.fromkeys([*target.tags, *other.tags]))
    target.uncertainty = list(dict.fromkeys([*target.uncertainty, *other.uncertainty]))
    target.source_refs = _dedupe_refs([*target.source_refs, *other.source_refs])


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
            absorbed.add(id(event))
            _absorb(target, event)

    if not merged_titles:
        return

    draft.events = [event for event in draft.events if id(event) not in absorbed]
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
    merged_titles: list[str] = []

    for event in draft.events:
        target = next((candidate for candidate in kept if _is_duplicate(candidate, event)), None)
        if target is None:
            kept.append(event)
            continue
        merged_titles.append(event.title)
        _absorb(target, event)

    draft.events = kept
    if merged_titles:
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


# --- 진입점 ------------------------------------------------------------------


def _filter_sources(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    source_stats = filter_draft_sources(draft, request)
    if source_stats.changed:
        logger.debug(
            "입력에 없는 rawId 참조 정리",
            extra=log_fields(
                **source_stats.violation_log_fields(item_kind="TIMELINE_EVENT")
            ),
        )


def _enforce_window(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    bounds = resolve_window_bounds(request)
    if bounds is not None:
        validate_draft_to_window(draft, bounds)


def _merge_stays(draft: TimelineDraft, request: TimelineDraftRequest) -> None:
    sort_events(draft)
    merge_stay_events(draft, request)


#: draft 를 고치는 단계. 이름은 보정 기록(`ConfirmReport`)에 그대로 실려 Repair 가 읽는다.
#:
#: 순서의 이유는 모듈 docstring 에 있다. 사진 단일 귀속이 맨 뒤인 것은 event 를 지우거나
#: 합치는 단계가 모두 끝난 뒤여야 하기 때문이다. 앞에 두면 곧 사라질 event 에 사진을 붙이고,
#: 병합으로 event 가 합쳐지면서 생긴 중복을 놓친다.
_CORRECTION_STEPS: tuple[
    tuple[str, Callable[[TimelineDraft, TimelineDraftRequest], object]], ...
] = (
    # 입력에 없는 rawId는 LLM 환각으로 본다. 잘못된 참조를 먼저 제거하고 유효한
    # 근거가 하나도 남지 않은 event는 이후 보정 단계로 넘기지 않는다.
    ("입력에 없는 근거 정리", _filter_sources),
    # 이후 모든 단계가 sourceRef 로 입력을 되짚으므로, LLM 이 붙인 sourceType 라벨을
    # 입력의 실제 타입으로 먼저 맞춘다. 라벨이 틀리면 근거를 영영 찾지 못한다.
    ("근거 타입 정정", normalize_source_types),
    # 되살린 캘린더 event 도 window·장소·정렬을 똑같이 거쳐야 하므로 여기서 채운다.
    ("캘린더 일정 복원", ensure_calendar_events),
    ("지속시간 복원", repair_durations),
    ("근거 구간 정렬", align_location_events),
    ("식사 시간", enforce_meal_duration),
    ("수면 경계", enforce_sleep_boundary),
    ("요청 시간 범위", _enforce_window),
    # 겹침 정리가 장소로 중복을 판별하므로, 그 전에 place 를 확정한다.
    ("장소 확정", resolve_places),
    ("체류 병합", _merge_stays),
    ("중복 정리", lambda draft, request: resolve_overlaps(draft)),
    ("사진 단일 귀속", enforce_photo_assignment),
    ("캘린더 장소 일치", reinforce_calendar_location),
)

#: v3 프롬프트가 정한 규칙으로 고치는 단계. v1·v2 세트에서는 돌리지 않는다.
#:
#: 대화 event 하루 3개는 Notification v3 가 정한 규칙이다(#116). 그 지시를 받은 적 없는
#: 세트가 만든 event 를 같은 규칙으로 지우면 운영 결과가 달라진다.
#:
#: 수면과 겹친 event 제거(#134)도 v3 가 수면 event 를 다루기 시작하면서 생긴 규칙이다.
#: v1·v2 는 수면을 그 세트대로 다루므로 건드리지 않는다. 사진 단일 귀속 뒤에 돌지만,
#: 지우는 event 의 사진·캘린더 근거는 수면 event 로 옮기므로 사진이 빠지지 않는다.
_EXTENDED_CORRECTION_STEPS: tuple[
    tuple[str, Callable[[TimelineDraft, TimelineDraftRequest], object]], ...
] = (
    ("수면과 겹친 event 제거", remove_events_overlapping_sleep),
    ("대화 개수 제한", enforce_conversation_limit),
    # 대화를 3개로 줄인 뒤에도 10개를 넘으면 근거가 알림뿐인 event 부터 지운다(#163).
    ("event 개수 정리", lambda draft, request: enforce_event_count(draft)),
    # 문장을 바꾸는 단계라 event 를 지우거나 합치는 단계가 모두 끝난 뒤에 둔다(#138).
    ("문장 주소 정리", replace_input_addresses),
)

#: v3 세트에서 `장소 확정` 바로 앞에 끼우는 단계(#138).
#:
#: 일정·사진 event 에 같은 시간의 체류를 위치 근거로 붙인다. 장소 확정보다 앞이어야 붙인
#: 체류로 비어 있는 place·address 가 같은 확정에서 채워진다. 붙인 체류는 사진 단일 귀속이
#: 그 event 의 근거로 치지 않으므로, 사진을 잃은 event 가 체류 카드로 살아남지 않는다.
_LOCATION_LINK_STEP: tuple[
    str, Callable[[TimelineDraft, TimelineDraftRequest], object]
] = ("위치 근거 연결", link_location_evidence)


def _resolve_places_without_filling(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> None:
    # v3 세트는 빈 place 를 후보로 채우지 않는다(#150). 고르는 것은 LLM 의 몫이다.
    resolve_places(draft, request, fill_empty=False)


def _correction_steps(
    extended: bool,
    candidates: Sequence[AiEventCandidate] = (),
    clues: TimelineClues | None = None,
) -> tuple[tuple[str, Callable[[TimelineDraft, TimelineDraftRequest], object]], ...]:
    # 사진 단일 귀속은 Timeline 이 버린 사진 candidate 를 되살려야 해서 candidate 를 받는다(#163).
    overrides: dict[str, Callable[[TimelineDraft, TimelineDraftRequest], object]] = {
        "사진 단일 귀속": partial(enforce_photo_assignment, candidates=candidates),
    }
    if extended:
        # v3 세트는 식당에 머문 식사를 3시간까지 두고(#163), 빈 place 를 채우지 않는다(#150).
        overrides["식사 시간"] = partial(enforce_meal_duration, allow_stay_meal=True)
        overrides["장소 확정"] = _resolve_places_without_filling
    if clues is not None:
        return _clue_correction_steps(overrides, clues)
    steps = tuple((name, overrides.get(name, step)) for name, step in _CORRECTION_STEPS)
    if not extended:
        return steps
    position = [name for name, _ in steps].index("장소 확정")
    return (
        *steps[:position],
        _LOCATION_LINK_STEP,
        *steps[position:],
        *_EXTENDED_CORRECTION_STEPS,
    )


#: v4 세트(#167·#171)에서 v3 의 단계 대신 도는 것과 빠지는 것.
#:
#: - `위치 근거 연결`(#138)은 돌리지 않는다. v4 는 근거 묶음이 이미 구간에 단서를 붙였다.
#: - 뼈대가 정한 구조를 지키는 네 단계를 더한다(`skeleton_guard`). 이동 기록도 교통 결제·예약도
#:   없는 MOVEMENT 를 지우고, 이동 구간의 기록을 다른 event 에서 빼고, 근거의 시간과 겹치지
#:   않는 event 를 지운 뒤, 어느 event 도 덮지 않은 기본 event 의 시간을 되살린다.
#: - `장소 확정` 은 장소 칸 확정이다. 근거가 이름을 가리키면 확정하고 아니면 후보를 정한다.
#: - `체류 병합` 은 돌리지 않는다. 위치 뼈대가 이미 같은 장소의 체류를 묶었다.
#: - `문장 주소 정리` 는 `문장 장소 칸` 이다. 이동 문장을 주소로 쓰고 칸을 맞춘다.
_CLUE_SKIPPED_STEPS = frozenset({"체류 병합"})


def _clue_correction_steps(
    overrides: dict[str, Callable[[TimelineDraft, TimelineDraftRequest], object]],
    clues: TimelineClues,
) -> tuple[tuple[str, Callable[[TimelineDraft, TimelineDraftRequest], object]], ...]:
    overrides = {
        **overrides,
        "장소 확정": partial(_confirm_place_slots, clues=clues),
        # 체류·이동의 시간은 뼈대 구간 안의 시간이다 — 원래 시각으로 맞추면 정리한 이동이 되늘어난다.
        "지속시간 복원": partial(repair_durations, skeleton=clues.skeleton),
        "근거 구간 정렬": partial(align_location_events, skeleton=clues.skeleton),
        # 사진 candidate 가 없어, 되살리는 사진은 이어 찍은 것끼리 묶고 같은 장면은 한 event 로 모은다.
        "사진 단일 귀속": enforce_photo_scenes,
        # 수면 일정은 계획이다 — 이동과 다른 장소의 체류는 겹쳐도 남긴다.
        "수면과 겹친 event 제거": partial(remove_events_overlapping_sleep, spare=spares_from_sleep(clues)),
        # v4 는 대화를 Notification 단서로 센다 — 앱 사전으로 세면 대화로 쓴 문자가 빠진다.
        # 단독 위치 기록은 보태기만 하는 근거라 알림뿐인 event 를 가를 때 세지 않는다(#173).
        "대화 개수 제한": partial(
            enforce_conversation_limit,
            conversation_ids={
                raw_id
                for clue in clues.notifications
                if clue.kind is NotificationClueKind.CONVERSATION
                for raw_id in clue.raw_ids
            },
            aside=clues.short_record_ids,
        ),
        "event 개수 정리": lambda draft, request: enforce_event_count(
            draft, aside=clues.short_record_ids
        ),
    }
    steps = tuple(
        (name, overrides.get(name, step))
        for name, step in _CORRECTION_STEPS
        if name not in _CLUE_SKIPPED_STEPS
    )
    # 위치 구간 복원은 캘린더 일정 복원 바로 뒤다. 되살린 event 도 시간·장소·정렬을 똑같이 거친다.
    # 일정 기본 event 지키기는 캘린더 일정 복원 바로 앞이다 — Timeline 이 일정 근거를 빠뜨린 일정
    # event 에 먼저 일정을 붙여야 복원이 같은 일정을 하나 더 만들지 않는다.
    position = [name for name, _ in steps].index("캘린더 일정 복원") + 1
    steps = (
        *steps[: position - 1],
        ("일정 기본 event 지키기", partial(_skeleton_step, keep_calendar_base_events, clues=clues)),
        *steps[position - 1 : position],
        # 뼈대가 정한 구조를 Timeline 이 어긴 것을 바로잡은 뒤에 빠진 구간을 복원한다.
        ("근거 없는 이동 정리", partial(_skeleton_step, remove_unrecorded_movements, clues=clues)),
        ("일정 근거 없는 일정 정리", partial(_skeleton_step, remove_calendar_events_without_calendar, clues=clues)),
        ("위치 근거 없는 체류 정리", partial(_skeleton_step, remove_stays_without_location, clues=clues)),
        ("이동 구간 분리", partial(_skeleton_step, separate_movement_segments, clues=clues)),
        ("근거 시간 밖 event 정리", partial(_skeleton_step, remove_events_outside_evidence, clues=clues)),
        ("위치 구간 복원", partial(_skeleton_step, ensure_skeleton_segments, clues=clues)),
        *steps[position:],
    )
    extended: list[tuple[str, Callable[[TimelineDraft, TimelineDraftRequest], object]]] = []
    for name, step in _EXTENDED_CORRECTION_STEPS:
        if name == "문장 주소 정리":
            # 캘린더 일정은 절대 빠지지 않는다(사용자 결정). 앞 단계가 일정 event 를 지웠으면 다시
            # 되살리고, 되살린 event 도 장소 후보를 받게 장소를 한 번 더 정한다.
            # 위치 구간도 같다 — 복원 뒤에 도는 식사 시간·수면 단계가 체류를 줄이거나 지우면 그 시간이
            # 빈다. 실제 재실행에서 407분 체류를 채운 식사가 20분으로 줄어 오후 체류가 통째로 비었다.
            extended.append(
                ("위치 구간 다시 확인", partial(_skeleton_step, ensure_skeleton_segments, clues=clues))
            )
            extended.append(("캘린더 일정 다시 확인", ensure_calendar_events))
            extended.append(("알림 event 복원", partial(_skeleton_step, ensure_notification_events, clues=clues)))
            # event 시간이 모두 정해진 뒤에 근거를 맞추고, 그 근거로 장소를 다시 정한다.
            extended.append(
                ("근거 정리", partial(_skeleton_step, settle_event_evidence, clues=clues))
            )
            # 근거 정리가 체류 근거를 빼면 알림뿐인 event 가 새로 생긴다(#173). 마지막 확정이면 그대로
            # 저장되므로 그 자리에서 다시 지운다.
            extended.append(
                ("알림 event 다시 정리", partial(_skeleton_step, remove_notification_events, clues=clues))
            )
            extended.append(("종류 없는 event 정리", partial(_skeleton_step, retire_unknown_events, clues=clues)))
            extended.append(("장소 다시 확정", partial(_confirm_place_slots, clues=clues)))
            # 글은 고치지 않는다(사용자 결정 2026-10-31). `_inspect` 가 제안으로 Repair 에 넘긴다.
            extended.append(
                ("되살린 event 넘기기", partial(_skeleton_step, hand_restored_events_to_repair, clues=clues))
            )
            # 예외는 장소 자리 둘이다. 문장의 장소 이름은 칸으로 바꿔야 사용자가 고른 장소를 따라가고,
            # 제목과 이동 글에는 장소 이름이 절대 남지 않는다(사용자 결정 2026-10-31).
            extended.append(("문장 장소 칸", partial(fill_place_slots, clues=clues, slots_only=True)))
            extended.append(("제목·이동 장소 이름", partial(_skeleton_step, keep_place_names_out, clues=clues)))
            extended.append(("제목 체류 표현", partial(_skeleton_step, keep_stay_word_out, clues=clues)))
        else:
            extended.append((name, overrides.get(name, step)))
            if name == "대화 개수 제한":
                # 알림은 틀의 근거다. 단독 event 로 남는 알림은 대화 3개뿐이다(#173).
                extended.append(
                    ("알림 event 정리", partial(_skeleton_step, remove_notification_events, clues=clues))
                )
    return (*steps, *extended)


def _confirm_place_slots(
    draft: TimelineDraft, request: TimelineDraftRequest, *, clues: TimelineClues
) -> None:
    confirm_place_slots(draft, request, clues)


def _skeleton_step(
    step: Callable[[TimelineDraft, TimelineDraftRequest, TimelineClues], None],
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    *,
    clues: TimelineClues,
) -> None:
    step(draft, request, clues)


def _inspect(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    report: ConfirmReport,
    *,
    extended: bool,
    clue_contract: bool = False,
    clues: TimelineClues | None = None,
) -> None:
    """고치지 않고 찾기만 하는 검사. 찾은 것은 warning 과 검사 결과로 남긴다.

    모든 병합·문장 수정이 끝난 결과를 잰다(#61, #118, #119). 자기 warning 을 가진 guard 는
    반복마다 이전 것을 지우고 현재 draft 로 다시 계산하므로, Repair 가 문장을 줄이거나
    event 를 나누고 합친 뒤 stale warning 이 남지 않는다.

    `extended` 가 거짓이면 #119 의 검사를 돌리지 않고 지속시간은 예전처럼 일괄 3시간,
    event 개수는 예전처럼 24개로 잰다. 찾은 것은 warning 으로 draft 에 남고 Repair 가
    그것을 읽기 때문이다. 나눌 도구도
    그 warning 을 읽는 법도 없는 세트에 "나눠야 합니다"를 보이면 Timeline 재실행만 되풀이한다.
    """

    # 강제 뒤에도 어긋난 사진이 있으면 드러낸다. 있어서는 안 되는 일이라 있으면 버그다.
    verify_photo_assignment(draft, request)
    # Notification Agent 결과를 통과한 뒤에도 Timeline/Repair가 문장을 다시 조립하면서
    # 민감정보나 근거 없는 관계명을 만들 수 있어 최종 draft를 한 번 더 검사한다.
    verify_notification_draft(draft, request)

    verify_narrative_length(draft)
    for finding in verify_event_duration(draft, request, by_type=extended):
        report.add_finding("DURATION_OVER_LIMIT", finding.detail(), event=finding.event)
    verify_event_count(
        draft, limit=MAX_EVENT_COUNT if extended else LEGACY_MAX_EVENT_COUNT
    )

    if not extended:
        return

    for found in verify_movement_stay_boundary(draft, request):
        # 나눌 자리는 event 의 시간 안으로 맞춘 값이라 최종 시간이 정해진 뒤에 만든다.
        report.add_finding("LONG_STAY_BETWEEN_MOVEMENTS", found.detail, event=found.event)

    # 겹치는 event 의 clientEventId 는 최종 id 가 매겨진 뒤에 굳힌다(#138).
    # v4 는 돌리지 않는다(사용자 결정 2026-10-31). 일정은 위치 구간에 넣었고 체류 안의 식사는 정상
    # 구조인데, 이 검사를 받은 Repair 가 7시간 체류를 식사로 고쳐 썼다.
    for overlap in [] if clue_contract else find_location_only_overlaps(draft, request):
        report.add_finding("LOCATION_ONLY_OVERLAP", overlap.detail, event=overlap.event)
    for meal in find_unsupported_meals(draft):
        report.add_finding(
            "UNSUPPORTED_MEAL",
            {"sourceTypes": sorted({ref.source_type.value for ref in meal.source_refs})},
            event=meal,
        )
    for label in find_narrative_labels(draft):
        # v4 는 주소를 문장에 쓸 수 있다(이동 문장, 후보가 없을 때의 주소 대체, 사용자 결정).
        if clue_contract and label.kind == "ADDRESS_IN_NARRATION":
            continue
        report.add_finding(label.kind, label.detail(), event=label.event)
    if clue_contract:
        # 제목에는 장소명을 쓰지 않는다(사용자 결정). 고칠 칸이 없어 Repair 에 넘긴다.
        for event in draft.events:
            if names := place_names_in_title(event, request, clues):
                report.add_finding("PLACE_NAME_IN_TITLE", {"found": names}, event=event)
        for event, conversations in conversations_on_other_events(draft, clues):
            report.add_finding("CONVERSATION_ON_OTHER_EVENT", {"found": conversations}, event=event)
        ordered = sorted(draft.events, key=lambda event: event.start_time)
        for event, following in zip(ordered, ordered[1:]):
            # 같은 일이면 괜찮다(사용자 결정 2026-10-31). 무엇이 다른지는 Repair 가 판단한다.
            # 활동을 모르는 체류끼리는 쓸 말이 없다. 짚자 Repair 가 `오후 체류` 를 지어냈다(#179).
            both_stays = event.event_type is EventType.STAY and following.event_type is EventType.STAY
            if event.title == following.title and event.event_type is not EventType.MOVEMENT and not both_stays:
                report.add_finding(
                    "SAME_TITLE_IN_A_ROW",
                    partial(lambda after: {"nextEvent": after.client_event_id}, following),
                    event=event,
                )
        for event in draft.events:
            if event.merge_reason == NEEDS_WRITING:
                report.add_finding(
                    "EVENT_TO_WRITE",
                    {"sourceTypes": sorted({ref.source_type.value for ref in event.source_refs})},
                    event=event,
                )
        for event, suggestion in suggest_text_fixes(draft, request, clues):
            if event.merge_reason != NEEDS_WRITING:
                report.add_finding("TEXT_CORRECTION", suggestion, event=event)


def repair_draft(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    *,
    report: ConfirmReport | None = None,
    extended: bool = True,
    issued_ids: set[str] | None = None,
    candidates: Sequence[AiEventCandidate] = (),
    clues: TimelineClues | None = None,
) -> TimelineDraft:
    """LLM 이 만든 draft 를 코드로 확정한다(in-place, 같은 객체를 돌려준다).

    `report` 를 주면 이번 확정에서 고친 것과 찾은 것을 거기에 적는다(#119). 주지 않아도
    확정 자체는 똑같이 돈다.

    `extended` 는 v3 프롬프트가 정한 규칙을 적용할지다. 거짓이면 #119 의 검사와 대화 개수
    제한을 돌리지 않는다. 사진 단일 귀속은 이 값과 무관하게 어느 세트에서든 강제한다.
    Repair Agent 가 프롬프트 세트를 보고 정해 넘긴다.

    `issued_ids` 는 지금까지 준 `clientEventId` 의 장부다(#144). 주면 이미 준 번호는
    그대로 두고 새 event 에만 쓴 적 없는 번호를 준다(`assign_event_ids`). Repair 가 반복마다
    넘겨, 도구 로그와 쌓인 보정 기록의 번호가 끝까지 같은 event 를 가리키게 한다. 주지
    않으면 1번부터 다시 매긴다.

    `candidates` 는 Event Agent 들의 candidate 다. 사진 단일 귀속이 Timeline 이 통째로 버린
    사진 candidate 를 사진 event 로 되살리는 데 쓴다(#163).

    `clues` 를 주면 v4 세트의 확정이다(#167·#171). 장소는 장소 칸으로 확정하고, 위치 근거
    연결과 체류 병합은 돌리지 않는다(`_clue_correction_steps`).
    """

    report = report if report is not None else ConfirmReport()

    for name, step in _correction_steps(extended, candidates, clues):
        report.run(name, draft, lambda step=step: step(draft, request))

    # source 하나를 여러 event가 근거로 사용할 수 있다. 현재는 timeline_items에
    # event별 source 스냅샷을 저장하고, 향후 N:M 연결 테이블이 이 관계를 맡는다.

    _inspect(draft, request, report, extended=extended, clue_contract=clues is not None, clues=clues)

    # 병합으로 event 구성이 바뀌었을 수 있어 한 번 더 정렬한 뒤 id 를 부여한다.
    # 번호는 여기서만 준다. 중간 guard 는 event 를 지워도 번호를 다시 매기지 않는다(#144).
    sort_events(draft)
    if issued_ids is None:
        renumber_events(draft)
    else:
        assign_event_ids(draft, issued_ids)
    report.finish(draft)

    logger.debug(
        "draft repair 완료: events=%d, warnings=%d",
        len(draft.events),
        len(draft.warnings),
    )
    return draft


def enforce_final_photo_assignment(
    draft: TimelineDraft,
    request: TimelineDraftRequest,
    candidates: Sequence[AiEventCandidate] = (),
    *,
    prefer_extracted: bool = False,
) -> None:
    """저장으로 넘어가기 직전에 사진 단일 귀속을 한 번 더 강제한다(#119).

    확정 pass 가 이미 강제했으므로 보통은 아무것도 하지 않는다. 확정 뒤에 draft 를 만지는
    단계가 생겨 사진 참조가 어긋나더라도, 어긋난 채로 저장되지는 않게 하는 마지막 자리다.
    저장을 실패시키지 않고 바로잡는다 — 실패로 막으면 사진 한 장 때문에 하루 기록 전체가
    사라진다.
    """

    # 남아 있는 event 의 번호는 바꾸지 않고 새로 만든 사진 event 에만 번호를 준다(#144).
    issued = {event.client_event_id for event in draft.events}
    enforcement = enforce_photo_assignment(
        draft, request, candidates, prefer_extracted=prefer_extracted
    )
    if enforcement.changed_composition:
        sort_events(draft)
        assign_event_ids(draft, issued)
