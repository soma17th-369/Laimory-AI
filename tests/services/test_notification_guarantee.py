"""틀이 있는 날 근거가 대화가 아닌 알림뿐인 event 는 남지 않는다(#173).

재실행에서 0건이어도 보장은 아니다. 무작위 draft 를 확정 pass 에 넣어 어떤 순서로 근거가 빠져도
그런 event 가 남지 않는지 본다. `FOUND_SEEDS` 는 `근거 정리` 가 체류 근거를 뺀 뒤 알림뿐인 event 가
남았던 입력이다.
"""

import random
from datetime import timedelta

import pytest

from app.schemas import EventSourceType, EventType
from app.schemas.clues import NotificationClue, NotificationClueKind
from app.services.conversation_guard import MAX_CONVERSATION_EVENTS, notification_only
from app.services.draft_repair import repair_draft
from tests.fixtures.requests import make_request, notification_item, photo_item, stay_item
from tests.services.test_v4_guards import _at, _clues, _draft, _event

FOUND_SEEDS = (203, 556, 708, 745, 767, 1075)

_KINDS = (
    NotificationClueKind.CONVERSATION,
    NotificationClueKind.NEWS,
    NotificationClueKind.PAYMENT,
    NotificationClueKind.RESERVATION,
    NotificationClueKind.ACTION_RESULT,
)
_TYPES = (
    EventType.UNKNOWN,
    EventType.SOCIAL,
    EventType.WORK,
    EventType.MEAL,
    EventType.CLASS,
    EventType.MOVEMENT,
    EventType.REST,
)


def _day(rng):
    stays = [
        stay_item("a", lat=37.50, start="2026-06-20T09:00:00", end="2026-06-20T11:00:00", places=["가"]),
        stay_item("b", lat=37.60, start="2026-06-20T13:00:00", end="2026-06-20T18:00:00", places=["나"]),
    ]
    photos = [photo_item("p", taken="2026-06-20T15:00:00")] if rng.random() < 0.5 else []
    items, clues = [], []
    for number, kind in enumerate(_KINDS * 2):
        hour, minute = rng.choice((8, 10, 12, 14, 16, 19, 21)), rng.choice((0, 5, 30, 45))
        item = notification_item(f"n{number}", "토스", f"알림{number}", posted=f"2026-06-20T{hour:02d}:{minute:02d}:00")
        items.append(item)
        clues.append(
            NotificationClue(
                id=f"N{number + 1}",
                kind=kind,
                rawIds=[item.raw_id],
                title="제목",
                summary="요약",
                firstAt=_at(hour, minute),
                lastAt=_at(hour, minute),
            )
        )
    return make_request(stays=stays, photos=photos, notifications=items), clues, stays, photos, items


def _random_event(rng, stays, photos, items, number):
    refs = [(EventSourceType.NOTIFICATION, item.raw_id) for item in rng.sample(items, rng.randint(0, 3))]
    if rng.random() < 0.5:
        refs.append((EventSourceType.STAY, rng.choice(stays).raw_id))
    if photos and rng.random() < 0.2:
        refs.append((EventSourceType.PHOTO, photos[0].raw_id))
    if not refs:
        refs.append((EventSourceType.NOTIFICATION, rng.choice(items).raw_id))
    start = _at(rng.randint(8, 21), rng.choice((0, 5, 20, 30, 45)))
    return _event(
        refs,
        start=start,
        end=start + timedelta(minutes=rng.choice((0, 0, 10, 30, 60, 180))),
        event_type=rng.choice(_TYPES),
        title=f"제목{number}",
        description="설명했다.",
        base=rng.choice(((), ("E1",), ("E2",), ("E3",))),
    )


@pytest.mark.parametrize("seed", [*range(250), *FOUND_SEEDS])
def test_a_framed_day_keeps_no_alert_event_but_conversations(seed):
    rng = random.Random(seed)
    request, clues, stays, photos, items = _day(rng)
    bundle = _clues(request, clues)
    talk = {raw for clue in clues if clue.kind is NotificationClueKind.CONVERSATION for raw in clue.raw_ids}
    draft = _draft(*[_random_event(rng, stays, photos, items, n) for n in range(rng.randint(1, 12))])

    repair_draft(draft, request, clues=bundle)

    alerts = [event for event in draft.events if notification_only(event, bundle.short_record_ids)]
    conversations = [e for e in alerts if any(r.raw_id in talk for r in e.source_refs)]
    assert len(conversations) <= MAX_CONVERSATION_EVENTS
    assert EventType.UNKNOWN not in {event.event_type for event in draft.events}
    if len(alerts) < len(draft.events):
        assert alerts == conversations
