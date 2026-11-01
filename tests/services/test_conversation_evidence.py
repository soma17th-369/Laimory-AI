"""대화 단서의 선정과 근거 연결 (#183).

대화 event 의 근거는 대화 알림뿐이다(사용자 결정 2026-11-02). 프롬프트만 말할 때는 실제 trace 입력에서
`from` 을 적으면 체류 기록이, 사진을 함께 적으면 사진을 옮긴 뒤 체류 기록이 대화 event 에 남았고, 그런
event 는 대화 3개 제한에도 세이지 않았다. 대화는 보조 근거로도 쓸 수 있어서 그 경로는 그대로인지도 본다.
"""

import json
from datetime import datetime, timedelta, timezone

from app.agents.timeline.timeline_agent import parse_timeline_clue_draft
from app.schemas import (
    ConversationDecision,
    EventSourceType,
    EventType,
    InferenceLevel,
    SourceRef,
    TimelineClueOutput,
    TimelineConversationReview,
    TimelineDraft,
    TimelineEventDraft,
)
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import build_timeline_clues, timeline_input_sections
from app.services.confirm_report import ConfirmReport
from app.services.conversation_guard import (
    enforce_conversation_limit,
    find_unused_conversations,
    is_conversation_event,
)
from app.services.draft_repair import repair_draft, repair_durations
from app.services.event_count_guard import enforce_event_count, verify_event_count
from app.services.location_skeleton import build_location_skeleton
from app.services.skeleton_guard import NEEDS_WRITING
from app.services.text_suggestions import suggest_text_fixes
from tests.fixtures.requests import (
    calendar_item,
    make_request,
    movement_item,
    notification_item,
    photo_item,
    stay_item,
)

_KST = timezone(timedelta(hours=9))


def _at(hour, minute=0):
    return datetime(2026, 6, 20, hour, minute, tzinfo=_KST)


def _posted(hour, minute=0):
    return f"2026-06-20T{hour:02d}:{minute:02d}:00"


def _talk(label, hour, minute=0, kind=NotificationClueKind.CONVERSATION, last=None):
    item = notification_item(label, "카카오톡", "친구", posted=_posted(hour, minute))
    clue = NotificationClue(
        id=label, kind=kind, rawIds=[item.raw_id], counterpart="친구", summary="주말 계획",
        firstAt=_at(hour, minute), lastAt=last or _at(hour, minute),
    )
    return item, clue


def _event(refs, *, start, end, event_type=EventType.SOCIAL, title="친구와 대화", base=(), reason=None):
    return TimelineEventDraft(
        client_event_id=f"event-{title}",
        event_type=event_type,
        title=title,
        description="친구에게서 메시지를 받았다.",
        start_time=start,
        end_time=end,
        confidence=0.7,
        inference_level=InferenceLevel.EVIDENCE_BASED,
        source_refs=[SourceRef(source_type=t, raw_id=r) for t, r in refs],
        base_event_ids=list(base),
        merge_reason=reason,
    )


def _draft(*events):
    return TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=list(events))


def _clues(request, notifications=(), photos=()):
    return build_timeline_clues(
        build_location_skeleton(request), build_calendar_clues(request), list(photos), list(notifications)
    )


def _frame(stay):
    return _event(
        [(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(18), event_type=EventType.STAY,
        title="보낸 시간", base=["E1"],
    )


def _holder(draft, item):
    return next(event for event in draft.events if item.raw_id in {r.raw_id for r in event.source_refs})


def _sources(event):
    return {ref.source_type for ref in event.source_refs}


# --- 대화 event 의 근거는 대화 알림뿐이다 -----------------------------------------------------------


def test_a_conversation_written_with_from_keeps_only_the_conversation():
    """`from` 을 적으면 그 구간의 체류 기록이 대화 event 의 근거가 됐다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 12)
    request = make_request(stays=[stay], notifications=[item])
    talk = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(12), end=_at(12), base=["E1"],
    )

    draft = repair_draft(_draft(_frame(stay), talk), request, clues=_clues(request, [clue]))

    kept = _holder(draft, item)
    assert _sources(kept) == {EventSourceType.NOTIFICATION}
    assert kept.base_event_ids == []
    assert (kept.start_time, kept.end_time) == (_at(12), _at(12))
    assert kept.merge_reason == NEEDS_WRITING
    assert any(
        stay.raw_id in {r.raw_id for r in event.source_refs} and event is not kept
        for event in draft.events
    )


def test_a_conversation_on_a_movement_is_not_deleted_with_it():
    """`from` 을 적은 대화가 이동 구간 분리에서 통째로 지워져 대화가 사라졌다."""

    home = stay_item("home", start=_posted(7), end=_posted(10), lat=37.5, lon=127.0)
    trip = movement_item("trip", start=_posted(10), end=_posted(11))
    office = stay_item("office", start=_posted(11), end=_posted(18), lat=37.6, lon=127.1)
    item, clue = _talk("N1", 10, 30)
    request = make_request(stays=[home, office], movements=[trip], notifications=[item])
    talk = _event(
        [(EventSourceType.MOVEMENT, trip.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(10, 30), end=_at(10, 30), base=["E2"],
    )

    draft = repair_draft(_draft(talk), request, clues=_clues(request, [clue]))

    assert _sources(_holder(draft, item)) == {EventSourceType.NOTIFICATION}


def test_a_conversation_that_lost_its_photo_keeps_only_the_conversation():
    """사진을 함께 적으면 사진은 식사로 옮겨지고 체류 기록과 대화만 남았다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    photo = photo_item("lunch", taken=_posted(12, 10))
    item, clue = _talk("N1", 12)
    request = make_request(stays=[stay], photos=[photo], notifications=[item])
    photo_clue = PhotoClue(clue_id="P1", raw_id=photo.raw_id, taken_at=_at(12, 10))
    meal = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.PHOTO, photo.raw_id)],
        start=_at(12, 5), end=_at(12, 30), event_type=EventType.MEAL, title="점심", base=["E1"],
    )
    talk = _event(
        [
            (EventSourceType.STAY, stay.raw_id),
            (EventSourceType.PHOTO, photo.raw_id),
            (EventSourceType.NOTIFICATION, item.raw_id),
        ],
        start=_at(12), end=_at(12), base=["E1"],
    )

    draft = repair_draft(
        _draft(_frame(stay), meal, talk), request, clues=_clues(request, [clue], [photo_clue])
    )

    assert _sources(_holder(draft, item)) == {EventSourceType.NOTIFICATION}
    assert EventSourceType.PHOTO in _sources(_holder(draft, photo))


def test_a_short_record_pulled_by_the_bundle_id_leaves_the_conversation():
    """대화의 묶음 id 를 적으면 시간이 겹치는 단독 위치 기록이 딸려 왔다."""

    short = stay_item("short", start=_posted(19), end=_posted(19, 5))
    item, clue = _talk("N1", 19, 2)
    request = make_request(stays=[short], notifications=[item])
    clues = _clues(request, [clue])
    assert clues.short_record_ids == {short.raw_id}
    talk = _event(
        [(EventSourceType.STAY, short.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(19, 2), end=_at(19, 2),
    )

    draft = repair_draft(_draft(talk), request, clues=clues)

    assert _sources(_holder(draft, item)) == {EventSourceType.NOTIFICATION}


def test_another_notification_leaves_the_conversation():
    """대화 아닌 알림이 붙으면 거래 내용이 대화 event 의 근거로 남았다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 12)
    pay, pay_clue = _talk("N2", 12, 5, kind=NotificationClueKind.PAYMENT)
    request = make_request(stays=[stay], notifications=[item, pay])
    talk = _event(
        [(EventSourceType.NOTIFICATION, item.raw_id), (EventSourceType.NOTIFICATION, pay.raw_id)],
        start=_at(12), end=_at(12, 5),
    )

    draft = repair_draft(_draft(_frame(stay), talk), request, clues=_clues(request, [clue, pay_clue]))

    kept = _holder(draft, item)
    assert {ref.raw_id for ref in kept.source_refs} == {item.raw_id}
    assert kept.merge_reason == NEEDS_WRITING


def test_a_conversation_takes_the_time_of_its_messages():
    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 12, last=_at(12, 40))
    request = make_request(stays=[stay], notifications=[item])
    talk = _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(9), end=_at(18))

    draft = repair_draft(_draft(_frame(stay), talk), request, clues=_clues(request, [clue]))

    kept = _holder(draft, item)
    assert (kept.start_time, kept.end_time) == (_at(12), _at(12, 40))
    assert kept.merge_reason is None


def test_conversations_with_a_location_count_toward_the_limit():
    """위치가 붙은 대화는 제한에 세이지 않아 대화 event 가 3개를 넘었다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    talks = [_talk(f"N{i}", 10 + i) for i in range(4)]
    request = make_request(stays=[stay], notifications=[item for item, _ in talks])
    events = [
        _event(
            [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
            start=_at(10 + i), end=_at(10 + i), title=f"대화{i}", base=["E1"],
        )
        for i, (item, _) in enumerate(talks)
    ]

    draft = repair_draft(
        _draft(_frame(stay), *events), request, clues=_clues(request, [clue for _, clue in talks])
    )

    assert sum(_sources(event) == {EventSourceType.NOTIFICATION} for event in draft.events) == 3


# --- 대화는 전체 10개와 상관없이 들어간다 (사용자 결정 2026-11-02) -----------------------------------


def _full_day(count):
    """사진 event 로 채운 하루. 근거가 알림뿐이 아니라 코드가 지우지 않고, 1시간 간격이라 합쳐지지 않는다."""

    photos = [photo_item(f"p{i}", taken=_posted(7 + i)) for i in range(count)]
    events = [
        _event(
            [(EventSourceType.PHOTO, photo.raw_id)],
            start=_at(7 + i), end=_at(7 + i), event_type=EventType.PHOTO_MOMENT, title=f"사진{i}",
        )
        for i, photo in enumerate(photos)
    ]
    return photos, events


def test_conversations_stay_on_a_day_with_ten_other_events():
    photos, events = _full_day(10)
    talks = [_talk(f"N{i}", 12 + i) for i in range(3)]
    notes = [item for item, _ in talks]
    request = make_request(photos=photos, notifications=notes)
    clues = _clues(request, [clue for _, clue in talks])
    conversations = [
        _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12 + i), end=_at(12 + i), title=f"대화{i}")
        for i, item in enumerate(notes)
    ]
    conversation_ids = {item.raw_id for item in notes}
    draft = _draft(*events, *conversations)

    assert enforce_event_count(draft, conversation_ids=conversation_ids) == []
    verify_event_count(draft, conversation_ids=conversation_ids)

    assert len(draft.events) == 13
    assert draft.warnings == []


def test_conversations_are_not_trimmed_for_other_notification_events():
    """10개를 넘으면 지우는 것은 대화가 아닌 알림 event 다. 대화가 더 적어도 대화는 남는다."""

    photos, events = _full_day(10)
    item, _ = _talk("N1", 12)
    pay, _ = _talk("N2", 13, kind=NotificationClueKind.PAYMENT)
    talk = _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12), end=_at(12), title="대화")
    alert = _event([(EventSourceType.NOTIFICATION, pay.raw_id)], start=_at(13), end=_at(13), title="결제")
    draft = _draft(*events, talk, alert)

    removed = enforce_event_count(draft, conversation_ids={item.raw_id})

    assert removed == [alert]
    assert talk in draft.events


def test_eleven_other_events_are_reported_without_the_conversations():
    photos, events = _full_day(11)
    item, _ = _talk("N1", 12)
    talk = _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12), end=_at(12), title="대화")
    draft = _draft(*events, talk)

    verify_event_count(draft, conversation_ids={item.raw_id})

    [warning] = draft.warnings
    assert "대화를 뺀 event 가 11개" in warning.message


def test_v3_still_counts_conversations_toward_the_limit():
    photos, events = _full_day(10)
    item, _ = _talk("N1", 12)
    talk = _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12), end=_at(12), title="대화")
    draft = _draft(*events, talk)

    assert enforce_event_count(draft) == [talk]


def test_confirm_keeps_three_conversations_past_ten_events():
    photos, events = _full_day(10)
    talks = [_talk(f"N{i}", 12 + i) for i in range(4)]
    request = make_request(photos=photos, notifications=[item for item, _ in talks])
    photo_clues = [
        PhotoClue(clue_id=f"P{i}", raw_id=photo.raw_id, taken_at=_at(7 + i)) for i, photo in enumerate(photos)
    ]
    conversations = [
        _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12 + i), end=_at(12 + i), title=f"대화{i}")
        for i, (item, _) in enumerate(talks)
    ]

    draft = repair_draft(
        _draft(*events, *conversations),
        request,
        clues=_clues(request, [clue for _, clue in talks], photo_clues),
    )

    assert sum(_sources(event) == {EventSourceType.NOTIFICATION} for event in draft.events) == 3
    assert sum(_sources(event) == {EventSourceType.PHOTO} for event in draft.events) == 10


def test_v4_keeps_the_longer_conversation_when_message_counts_tie():
    """한 건짜리 대화끼리는 먼저 시작한 것이 남아 짧은 한마디가 긴 조언을 밀어냈다."""

    talks = [_talk(f"N{i}", 10 + i) for i in range(4)]
    talks[3][0].text = "성공의 부담을 덜고 기회가 있을 때 도전하자는 긴 조언이다."
    request = make_request(notifications=[item for item, _ in talks])
    events = [
        _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(10 + i), end=_at(10 + i), title=f"대화{i}")
        for i, (item, _) in enumerate(talks)
    ]
    draft = _draft(*events)

    [removed] = enforce_conversation_limit(draft, request, {item.raw_id for item, _ in talks})

    assert removed.title == "대화2"


def test_v3_keeps_the_earlier_conversation_when_message_counts_tie():
    talks = [
        notification_item(f"c{i}", "카카오톡", f"친구{i}", posted=_posted(10 + i)) for i in range(4)
    ]
    talks[3].text = "성공의 부담을 덜고 기회가 있을 때 도전하자는 긴 조언이다."
    request = make_request(notifications=talks)
    events = [
        _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(10 + i), end=_at(10 + i), title=f"대화{i}")
        for i, item in enumerate(talks)
    ]

    [removed] = enforce_conversation_limit(_draft(*events), request)

    assert removed.title == "대화3"


# --- 대화는 받은 메시지만 말한다 -------------------------------------------------------------------


def _suggested(description):
    item, clue = _talk("N1", 12)
    request = make_request(notifications=[item])
    talk = _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12), end=_at(12))
    talk.description = description
    draft = _draft(talk)
    return suggest_text_fixes(draft, request, _clues(request, [clue])), draft


def test_two_way_wording_is_suggested_not_rewritten():
    """코드는 글을 고치지 않는다. 사본에서 찾아 Repair 에 넘긴다."""

    [(_, suggestion)], draft = _suggested("친구에게서 설정 관련 이야기를 주고받았다.")

    assert suggestion["description"] == "친구에게서 설정 관련 메시지를 받았다."
    assert suggestion["rules"] == ["대화는 받은 메시지만 말한다"]
    assert draft.events[0].description == "친구에게서 설정 관련 이야기를 주고받았다."


def test_a_topic_before_the_verb_loses_its_particle():
    [(_, suggestion)], _ = _suggested("친구와 주말 계획을 이야기했다.")

    assert suggestion["description"] == "친구와 주말 계획 메시지를 받았다."


def test_received_wording_is_left_alone():
    assert _suggested("친구에게서 주말 계획 메시지를 받았다.")[0] == []


def test_a_meeting_may_say_they_talked():
    """일정·사진이 있는 만남은 대화 event 가 아니다."""

    photo = photo_item("p", taken=_posted(12))
    item, clue = _talk("N1", 12)
    request = make_request(photos=[photo], notifications=[item])
    meeting = _event(
        [(EventSourceType.PHOTO, photo.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(12), end=_at(12),
    )
    meeting.description = "친구와 이야기를 나눴다."

    assert suggest_text_fixes(_draft(meeting), request, _clues(request, [clue])) == []


# --- 다른 event 를 뒷받침하는 대화는 그대로다 ------------------------------------------------------


def test_a_conversation_stays_on_a_calendar_event_it_supports():
    meeting = calendar_item("m", "프로젝트 회의", start=_posted(14), end=_posted(15))
    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 13, 40)
    request = make_request(stays=[stay], calendars=[meeting], notifications=[item])
    event = _event(
        [(EventSourceType.CALENDAR, meeting.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(14), end=_at(15), event_type=EventType.CALENDAR_EVENT, title="프로젝트 회의",
    )

    draft = repair_draft(_draft(_frame(stay), event), request, clues=_clues(request, [clue]))

    assert _sources(_holder(draft, item)) == {EventSourceType.CALENDAR, EventSourceType.NOTIFICATION}


def test_a_conversation_stays_on_a_stay_it_supports():
    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 12)
    request = make_request(stays=[stay], notifications=[item])
    frame = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.STAY, title="보낸 시간", base=["E1"],
    )

    draft = repair_draft(_draft(frame), request, clues=_clues(request, [clue]))

    assert _sources(_holder(draft, item)) == {EventSourceType.STAY, EventSourceType.NOTIFICATION}


def test_a_meeting_with_a_photo_is_not_a_conversation_event():
    photo = photo_item("p", taken=_posted(12))
    item, _ = _talk("N1", 12)
    event = _event(
        [(EventSourceType.PHOTO, photo.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(12), end=_at(12),
    )

    assert not is_conversation_event(event, {item.raw_id})


def test_a_social_event_with_only_a_stay_and_a_conversation_is_a_conversation_event():
    """받은 메시지와 그 시간의 체류만으로는 만났다고 할 수 없다."""

    stay = stay_item("office")
    item, _ = _talk("N1", 12)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(12), end=_at(12),
    )

    assert is_conversation_event(event, {item.raw_id})


def test_v3_leaves_a_conversation_with_a_stay_alone():
    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item = notification_item("chat", "카카오톡", "친구", posted=_posted(12))
    request = make_request(stays=[stay], notifications=[item])
    talk = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(12), end=_at(12),
    )

    draft = repair_draft(_draft(talk), request)

    assert EventSourceType.STAY in _sources(_holder(draft, item))


# --- 쓰이지 않은 대화를 찾는다 -------------------------------------------------------------------


def test_finds_conversations_none_of_which_are_used():
    stay = stay_item("office")
    _, first = _talk("N1", 10)
    _, second = _talk("N2", 11)
    frame = _event([(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(18), event_type=EventType.STAY)

    [found] = find_unused_conversations(_draft(frame), [first, second])

    assert found.event is None
    assert found.detail() == {"conversationIds": ["N1", "N2"]}


def test_one_used_conversation_is_enough():
    stay = stay_item("office")
    item, first = _talk("N1", 10)
    _, second = _talk("N2", 11)
    frame = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.STAY,
    )

    assert find_unused_conversations(_draft(frame), [first, second]) == []


def test_finds_nothing_without_conversations():
    assert find_unused_conversations(_draft(), []) == []


def _review(identifier, decision):
    return TimelineConversationReview(id=identifier, decision=decision)


def test_finds_a_conversation_decided_on_but_not_linked():
    """남기기로 정하고 event 를 쓰다 빠뜨린 대화다. 다른 대화가 쓰였어도 찾는다."""

    stay = stay_item("office")
    item, first = _talk("N1", 10)
    _, second = _talk("N2", 11)
    _, third = _talk("N3", 12)
    frame = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.STAY,
    )
    draft = _draft(frame)
    draft.conversation_reviews = [
        _review("N1", ConversationDecision.SUPPORT),
        _review("N2", ConversationDecision.RECORD),
        _review("N3", ConversationDecision.LEAVE_OUT),
    ]

    [found] = find_unused_conversations(draft, [first, second, third])

    assert found.conversation_ids == ["N2"]


def test_a_decision_to_leave_everything_out_is_still_reviewed():
    """전부 제외는 Repair 가 다시 본다(이슈). 타당하면 그대로 둘 수 있다."""

    _, first = _talk("N1", 10)
    draft = _draft()
    draft.conversation_reviews = [_review("N1", ConversationDecision.LEAVE_OUT)]

    [found] = find_unused_conversations(draft, [first])

    assert found.conversation_ids == ["N1"]


def test_a_left_out_conversation_is_not_found_when_others_are_used():
    stay = stay_item("office")
    item, first = _talk("N1", 10)
    _, second = _talk("N2", 11)
    frame = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.STAY,
    )
    draft = _draft(frame)
    draft.conversation_reviews = [_review("N2", ConversationDecision.LEAVE_OUT)]

    assert find_unused_conversations(draft, [first, second]) == []


def test_conversation_reviews_are_parsed_but_never_dumped():
    """v3 Repair 프롬프트가 draft 를 그대로 싣는다. 결과 저장 계약에도 없다."""

    item, clue = _talk("N1", 12)
    request = make_request(notifications=[item])
    text = json.dumps(
        {
            "conversations": [
                {"id": "N1", "content": "주말 계획", "reason": "구체적이다", "decision": "RECORD"},
                {"id": "N2", "decision": "없는 값"},
            ],
            "events": [],
        }
    )

    draft = parse_timeline_clue_draft(text, request, _clues(request, [clue]))

    assert [(r.clue_id, r.decision) for r in draft.conversation_reviews] == [("N1", ConversationDecision.RECORD)]
    assert "conversationReviews" not in draft.model_dump(by_alias=True, mode="json")
    assert draft.model_copy(deep=True).conversation_reviews == draft.conversation_reviews


def _parse(reviews, request, clues, events=()):
    return parse_timeline_clue_draft(
        json.dumps({"conversations": reviews, "events": list(events)}), request, clues
    )


def _recorded(identifier="N1", title="친구와 대화", description="친구에게서 주말 계획 메시지를 받았다."):
    return {
        "id": identifier, "content": "주말 계획", "reason": "구체적이다", "decision": "RECORD",
        "title": title, "description": description,
    }


def test_a_recorded_conversation_becomes_an_event_of_its_own():
    """`RECORD` 로 정하고도 `events` 에 쓰지 않았다. 판단과 글은 Timeline, 구조는 코드다."""

    item, clue = _talk("N1", 12, last=_at(12, 30))
    request = make_request(notifications=[item])

    [event] = _parse([_recorded()], request, _clues(request, [clue])).events

    assert event.event_type is EventType.SOCIAL
    assert (event.title, event.description) == ("친구와 대화", "친구에게서 주말 계획 메시지를 받았다.")
    assert (event.start_time, event.end_time) == (_at(12), _at(12, 30))
    assert [ref.raw_id for ref in event.source_refs] == [item.raw_id]
    assert event.base_event_ids == []
    assert event.merge_reason == "구체적이다"


def test_a_recorded_conversation_without_text_is_written_by_repair():
    """판단은 있는데 글을 비우면 event 가 사라졌다. 구조는 코드가 만들고 글은 Repair 에 맡긴다."""

    item, clue = _talk("N1", 12)
    request = make_request(notifications=[item])

    [event] = _parse([_recorded(title="", description="")], request, _clues(request, [clue])).events

    assert (event.title, event.description) == ("친구", "주말 계획")
    assert event.merge_reason == NEEDS_WRITING
    assert [ref.raw_id for ref in event.source_refs] == [item.raw_id]


def test_only_a_recorded_conversation_becomes_an_event():
    item, clue = _talk("N1", 12)
    pay, pay_clue = _talk("N2", 13, kind=NotificationClueKind.PAYMENT)
    request = make_request(notifications=[item, pay])
    reviews = [
        {**_recorded(), "decision": "SUPPORT"},
        _recorded("N2"),
        _recorded("N9"),
    ]

    assert _parse(reviews, request, _clues(request, [clue, pay_clue])).events == []


def test_a_recorded_conversation_timeline_already_wrote_is_not_doubled():
    item, clue = _talk("N1", 12)
    request = make_request(notifications=[item])
    written = {
        "eventType": "SOCIAL", "title": "친구와 대화", "description": "메시지를 받았다.", "from": [], "evidence": ["N1"],
        "startTime": _at(12).isoformat(), "endTime": _at(12).isoformat(), "confidence": 0.7,
        "inferenceLevel": "DIRECT", "uncertainty": [], "mergeReason": "", "placeCandidates": [],
    }

    draft = _parse([_recorded()], request, _clues(request, [clue]), [written])

    assert len(draft.events) == 1


def test_recorded_conversations_keep_to_the_limits():
    """개수로 빠진 대화는 다시 볼 대화로 찾지 않는다. 다시 보여도 도구가 거절할 뿐이다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    talks = [_talk(f"N{i}", 10 + i) for i in range(1, 6)]
    request = make_request(stays=[stay], notifications=[item for item, _ in talks])
    clues = _clues(request, [clue for _, clue in talks])
    frame = {
        "eventType": "STAY", "title": "보낸 시간", "description": "{place}에서 시간을 보냈다.", "from": ["E1"],
        "evidence": [], "startTime": _at(9).isoformat(), "endTime": _at(18).isoformat(), "confidence": 0.7,
        "inferenceLevel": "EVIDENCE_BASED", "uncertainty": [], "mergeReason": "", "placeCandidates": [],
    }
    draft = _parse([_recorded(clue.clue_id) for _, clue in talks], request, clues, [frame])
    report = ConfirmReport()

    repair_draft(draft, request, report=report, clues=clues)

    assert sum(_sources(event) == {EventSourceType.NOTIFICATION} for event in draft.events) == 3
    assert not [f for f in report.findings if f["kind"] == "CONVERSATIONS_UNUSED"]


def test_a_conversation_meant_to_support_is_found_even_on_a_full_day():
    stay = stay_item("office")
    _, clue = _talk("N1", 10)
    frame = _event([(EventSourceType.STAY, stay.raw_id)], start=_at(9), end=_at(18), event_type=EventType.STAY)
    draft = _draft(frame)
    draft.conversation_reviews = [_review("N1", ConversationDecision.SUPPORT)]

    [found] = find_unused_conversations(draft, [clue], room=False)

    assert found.conversation_ids == ["N1"]


def test_conversations_come_before_events_in_the_output_schema():
    """모델은 schema 순서로 쓴다. 뒤에 두면 event 를 다 쓰고 대화를 몰아 처리한다."""

    properties = list(TimelineClueOutput.model_json_schema()["properties"])

    assert properties.index("conversations") < properties.index("events")


def test_finds_a_conversation_named_but_not_linked():
    """이유에 `N2` 를 쓰고 근거로 잇지 않으면 설명과 연결이 어긋난다."""

    stay = stay_item("office")
    item, first = _talk("N1", 10)
    _, second = _talk("N2", 11)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.STAY, reason="N1 과 N2 가 회의 주제를 말한다.",
    )

    [found] = find_unused_conversations(_draft(event), [first, second])

    assert found.event is event
    assert found.conversation_ids == ["N2"]


def test_an_id_that_is_not_a_conversation_is_ignored():
    stay = stay_item("office")
    item, first = _talk("N1", 10)
    event = _event(
        [(EventSourceType.STAY, stay.raw_id), (EventSourceType.NOTIFICATION, item.raw_id)],
        start=_at(9), end=_at(18), event_type=EventType.STAY, reason="N1, N15, BN2",
    )

    assert find_unused_conversations(_draft(event), [first]) == []


def test_confirm_reports_unused_conversations_in_v4_only():
    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 12)
    request = make_request(stays=[stay], notifications=[item])
    report = ConfirmReport()

    repair_draft(_draft(_frame(stay)), request, report=report, clues=_clues(request, [clue]))
    assert [f["conversationIds"] for f in report.findings if f["kind"] == "CONVERSATIONS_UNUSED"] == [["N1"]]

    legacy = ConfirmReport()
    repair_draft(_draft(_frame(stay)), request, report=legacy)
    assert not [f for f in legacy.findings if f["kind"] == "CONVERSATIONS_UNUSED"]


def test_a_conversation_cut_by_the_limit_is_told_apart_from_one_left_out():
    """개수로 지운 대화는 코드가 그 사실을 남기고, 다시 볼 대화로 찾지 않는다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    talks = [_talk(f"N{i}", 10 + i) for i in range(4)]
    request = make_request(stays=[stay], notifications=[item for item, _ in talks])
    events = [
        _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(10 + i), end=_at(10 + i), title=f"대화{i}")
        for i, (item, _) in enumerate(talks)
    ]
    report = ConfirmReport()

    draft = repair_draft(
        _draft(_frame(stay), *events), request, report=report, clues=_clues(request, [c for _, c in talks])
    )

    assert any("알림이 많은 3건만" in warning.message for warning in draft.warnings)
    assert [r["step"] for r in report.removed] == ["대화 개수 제한"]
    assert not [f for f in report.findings if f["kind"] == "CONVERSATIONS_UNUSED"]


# --- 한 건짜리 대화 --------------------------------------------------------------------------


def test_a_one_message_conversation_is_a_moment_in_v4():
    """메시지 한 건인 대화에 `복원할 수 없음` warning 이 붙어 Repair 가 문제로 읽었다."""

    stay = stay_item("office", start=_posted(9), end=_posted(18))
    item, clue = _talk("N1", 12)
    request = make_request(stays=[stay], notifications=[item])
    talk = _event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12), end=_at(12))

    draft = repair_draft(_draft(_frame(stay), talk), request, clues=_clues(request, [clue]))

    assert not any("복원할 수 없는" in warning.message for warning in draft.warnings)
    assert _holder(draft, item).uncertainty == []


def test_v3_still_reports_a_moment_it_cannot_restore():
    item = notification_item("chat", "카카오톡", "친구", posted=_posted(12))
    request = make_request(notifications=[item])
    draft = _draft(_event([(EventSourceType.NOTIFICATION, item.raw_id)], start=_at(12), end=_at(12)))

    repair_durations(draft, request)

    assert any("복원할 수 없는" in warning.message for warning in draft.warnings)


# --- Timeline 의 판단이 Repair 까지 닿는다 ---------------------------------------------------------


def test_a_warning_keeps_the_reason_the_model_wrote():
    """이유가 `대화 알림` 으로 덮여 Repair 가 왜 뺐는지 읽을 수 없었다."""

    item, clue = _talk("N1", 12)
    request = make_request(notifications=[item])
    text = json.dumps(
        {
            "events": [],
            "warnings": [
                {
                    "message": "이모티콘만 있어 뺐다.",
                    "sourceRefs": [
                        {"sourceType": "NOTIFICATION", "rawId": "N1", "reason": "기록할 내용이 없다."}
                    ],
                }
            ],
        }
    )

    draft = parse_timeline_clue_draft(text, request, _clues(request, [clue]))

    [ref] = draft.warnings[0].source_refs
    assert (ref.raw_id, ref.reason) == (item.raw_id, "기록할 내용이 없다.")


def test_a_warning_without_a_reason_still_expands_the_id():
    item, clue = _talk("N1", 12)
    request = make_request(notifications=[item])
    text = json.dumps({"events": [], "warnings": [{"message": "뺐다.", "sourceRefs": ["N1"]}]})

    draft = parse_timeline_clue_draft(text, request, _clues(request, [clue]))

    assert [ref.raw_id for ref in draft.warnings[0].source_refs] == [item.raw_id]


def test_a_conversation_shows_how_many_messages_it_has():
    """프롬프트는 메시지가 많은 대화부터 고르라는데 입력에 그 값이 없었다."""

    first = notification_item("a", "카카오톡", "친구", posted=_posted(12))
    second = notification_item("b", "카카오톡", "친구", posted=_posted(12, 5))
    request = make_request(notifications=[first, second])
    clue = NotificationClue(
        id="N1", kind=NotificationClueKind.CONVERSATION, rawIds=[first.raw_id, second.raw_id],
        firstAt=_at(12), lastAt=_at(12, 5),
    )
    pay, pay_clue = _talk("N2", 13, kind=NotificationClueKind.PAYMENT)

    text = json.dumps(timeline_input_sections(_clues(request, [clue, pay_clue])), ensure_ascii=False, default=str)

    assert text.count('"messageCount"') == 1
    assert '"messageCount": 2' in text
