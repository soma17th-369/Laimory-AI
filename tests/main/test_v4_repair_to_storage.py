"""Repair 를 거쳐 저장 요청까지 코드가 정한 하루의 구조가 남는다 (#203, v4).

#202 는 Timeline 을 부르기 전에 하루의 event 와 그 시간·위치 기록·사진·일정 귀속을 코드로 정했다. 이 파일은
그 구조가 Repair 의 편집, 반복되는 확정, 저장 직전 마무리를 지나 App Server 가 받는 저장 요청 body 까지 같은지
본다. 운영 사례(백화점 장식·디저트 사진, 같은 공간의 실내·입구 사진)는 합성 좌표와 가상 이름으로 옮겼다.

가짜 LLM 의 Repair 계획은 운영에서 본 잘못된 편집을 반복한다 — 방문의 종류를 바꾸고 6조각으로 나누고, 장식 사진을
다른 event 로 보내고, 시간을 자르고, 방문을 지운다. 실제 runner(`process_timeline_task`)를 돌리고 실제 LLM 은
부르지 않는다.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.events.notification.clue_agent import NotificationClueAgent
from app.agents.events.photo.clue_agent import PhotoClueAgent
from app.agents.events.photo.describer import PhotoDescriber
from app.agents.main import main_agent, run_main_agent
from app.agents.repair.repair_agent import RepairAgent
from app.agents.timeline.timeline_agent import TimelineAgent
from app.schemas import CalendarItem, CollectedSourceItem, EventSourceType, ItemType, SourceRef, TaskStatus, TimelineDraft
from app.schemas.clues import PhotoClue
from app.services import normalizer, timeline_runner
from app.services.base_events import structural_refs
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import build_timeline_clues
from app.services.draft_repair import enforce_final_photo_assignment, repair_draft
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.app_server import FakeAppServerClient
from tests.fixtures.fake_llm import FakeLLM
from tests.fixtures.pipeline import silent_question_agent
from tests.fixtures.requests import calendar_item, fixture_raw_id, make_request, make_snapshot, photo_item, stay_item

_KST = timezone(timedelta(hours=9))
_TASK_ID = "task-203"
_TOKEN = "token-203"
_LOCATION = (EventSourceType.STAY, EventSourceType.MOVEMENT)
#: 확정마다 반복하는 Repair 차례. 쓸 글이 남아 있으면 Repair 는 상한까지 돈다.
_ITERATIONS = 3


class _NoDescribe(PhotoDescriber):
    def describe(self, photos):
        return {}


@pytest.fixture(autouse=True)
def clue_contract(monkeypatch):
    monkeypatch.setattr(main_agent, "uses_clue_contract", lambda: True)
    monkeypatch.setattr(normalizer, "uses_clue_contract", lambda: True)


def _t(hhmm: str) -> str:
    return f"2026-06-20T{hhmm}:00"


def _iso(hhmm: str) -> str:
    return f"2026-06-20T{hhmm}:00+09:00"


# --- 하루 ---------------------------------------------------------------------------------------


class _Day:
    """입력 하나와 사진 해석. `photos` 는 사진 id(촬영 순서대로 P1…) → (입력 항목, 사진이 보여 주는 일)."""

    def __init__(self, *, stays=(), calendars=(), photos=()):
        self.photos = {label: (item, activity) for label, item, activity in photos}
        self.request = make_request(
            stays=list(stays), calendars=list(calendars), photos=[item for item, _ in self.photos.values()]
        )

    def clues(self):
        """코드의 기준. 파이프라인이 만드는 것과 같은 입력으로 만든다."""

        photo_clues = [
            PhotoClue(
                clue_id=label, raw_id=item.raw_id, taken_at=_taken(item.taken_at),
                latitude=item.latitude, longitude=item.longitude, places=list(item.places), activity=activity,
            )
            for label, (item, activity) in self.photos.items()
        ]
        return build_timeline_clues(
            build_location_skeleton(self.request), build_calendar_clues(self.request), photo_clues, []
        )

    def photo_id(self, label: str) -> str:
        return self.photos[label][0].raw_id

    def split(self, raw_ids) -> tuple[tuple, tuple, tuple]:
        """rawId 를 (위치 기록, 사진, 일정) 으로 가른다. 알림은 모델이 고르는 근거라 넣지 않는다."""

        types = {item.raw_id: EventSourceType.STAY for item in self.request.stays}
        types.update({item.raw_id: EventSourceType.PHOTO for item in self.request.photos})
        types.update({item.raw_id: EventSourceType.CALENDAR for item in self.request.calendars})
        raw_ids = list(raw_ids)
        return tuple(tuple(sorted(i for i in raw_ids if types.get(i) in group)) for group in _GROUPS)


def _taken(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text).replace(tzinfo=_KST)
    except ValueError:
        return None


def _photo(label, hhmm, activity, *, lat=None, lon=None, places=()):
    return label, photo_item(label, taken=_t(hhmm), lat=lat, lon=lon, places=list(places)), activity


def _two_visits_day() -> _Day:
    """백화점에서 장식 둘과 디저트, 이어서 음악감상실에서 시간 간격을 둔 실내·입구 사진(운영 사례)."""

    store = dict(places=["OO백화점"], address="서울 중구 명동 1")
    hall = dict(places=["XX음악감상실"], address="서울 중구 을지로 2")
    return _Day(
        stays=[
            stay_item("store-1", lat=37.5000, lon=127.0000, start=_t("10:47"), end=_t("11:52"), **store),
            stay_item("store-2", lat=37.5002, lon=127.0001, start=_t("13:43"), end=_t("14:16"), **store),
            stay_item("store-3", lat=37.5007, lon=126.9995, start=_t("14:33"), end=_t("15:24"), **store),
            stay_item("hall-1", lat=37.5060, lon=127.0070, start=_t("15:32"), end=_t("16:00"), **hall),
            stay_item("hall-2", lat=37.5061, lon=127.0081, start=_t("17:13"), end=_t("17:37"), **hall),
            stay_item("hall-3", lat=37.5056, lon=127.0076, start=_t("17:44"), end=_t("19:08"), **hall),
        ],
        photos=[
            _photo("P1", "14:39", "크리스마스 장식 앞에서 시간을 보냈다", lat=37.5001, lon=127.0000),
            _photo("P2", "14:41", "크리스마스 장식 앞에 다녀왔다", lat=37.5000, lon=126.9999),
            _photo("P3", "15:01", "망고 디저트를 먹었다", lat=37.5008, lon=126.9994),
            _photo("P4", "15:50", "음악감상실 실내에서 시간을 보냈다", lat=37.5063, lon=127.0072),
            _photo("P5", "17:19", "음악감상실 입구에 다녀왔다", lat=37.5062, lon=127.0080),
        ],
    )


def _all_day(label: str, title: str) -> CalendarItem:
    return CalendarItem(
        raw_id=fixture_raw_id(f"calendar-{label}"), start_at=_t("00:00"), end_at="2026-06-21T00:00:00", title=title, all_day=True
    )


def _calendar_day(*, all_day: bool = True, unreadable_photo: bool = False) -> _Day:
    """사무실 방문 일부에 걸친 같은 시각의 일정 둘, 종일 일정 둘, 방문이 없는 저녁의 독립 일정."""

    calendars = [
        calendar_item("meeting", "주간 회의", start=_t("14:00"), end=_t("15:00")),
        calendar_item("call", "고객 통화", start=_t("14:00"), end=_t("15:00")),
        calendar_item("study", "온라인 스터디", start=_t("20:00"), end=_t("21:00")),
    ]
    if all_day:
        calendars += [_all_day("holiday", "기념일"), _all_day("trip", "부산 출장")]
    photos = [
        _photo("P1", "14:30", "화이트보드 앞에서 시간을 보냈다", lat=37.5001, lon=127.0000),
        _photo("P2", "20:30", "노트북 화면 앞에서 공부했다"),
    ]
    if unreadable_photo:
        photos.append(("P3", photo_item("odd", taken="not-a-time"), "사진을 남겼다"))
    return _Day(
        stays=[
            stay_item("office", lat=37.5000, lon=127.0000, start=_t("09:00"), end=_t("18:00"), places=["OO타워"], address="서울 마포구 도화동 1")
        ],
        calendars=calendars,
        photos=photos,
    )


def _revisit_day() -> _Day:
    """같은 카페에 점심과 저녁에 따로 갔다. 사이에 멀리 떨어진 사무실에 있었다."""

    cafe = dict(places=["OO카페"], address="서울 마포구 연남동 3")
    return _Day(
        stays=[
            stay_item("cafe-noon", lat=37.5600, lon=126.9200, start=_t("12:00"), end=_t("13:00"), **cafe),
            stay_item("office", lat=37.5000, lon=127.0300, start=_t("13:40"), end=_t("17:00"), places=["OO타워"], address="서울 강남구 역삼동 5"),
            stay_item("cafe-night", lat=37.5600, lon=126.9200, start=_t("18:00"), end=_t("19:00"), **cafe),
        ],
        photos=[
            _photo("P1", "12:30", "라떼를 마셨다", lat=37.5601, lon=126.9201),
            _photo("P2", "18:30", "케이크를 먹었다", lat=37.5601, lon=126.9201),
        ],
    )


# --- 파이프라인 ----------------------------------------------------------------------------------


def _snapshot(request):
    """요청 항목을 입력 조회 응답의 sourceItems 로 되돌린다. 정규화가 같은 항목을 다시 만든다."""

    groups = [
        (ItemType.STAY, request.stays, "startAt"),
        (ItemType.CALENDAR, request.calendars, "startAt"),
        (ItemType.PHOTO, request.photos, "takenAt"),
    ]
    items = []
    for item_type, entries, start_key in groups:
        for entry in entries:
            payload = entry.model_dump(by_alias=True, mode="json", exclude_none=True)
            raw_id = payload.pop("rawId")
            start = payload.pop(start_key)
            end = payload.pop("endAt", None)
            items.append(CollectedSourceItem(raw_id=raw_id, item_type=item_type, start_at=start, end_at=end, payload=payload))
    return make_snapshot(task_id=_TASK_ID, source_items=items)


def _plan(*calls) -> str:
    return json.dumps(
        {
            "issues": [],
            "toolCalls": [{"tool": tool, "args": args, "reason": "운영에서 본 잘못된 편집"} for tool, args in calls],
            "done": False,
            "summary": "",
        },
        ensure_ascii=False,
    )


def _narrations(day: _Day, writer) -> str:
    return json.dumps(
        {"conversations": [], "events": [writer(base) for base in day.clues().base_events], "warnings": []},
        ensure_ascii=False,
    )


def _plain(base) -> dict:
    return {
        "id": base.base_id, "eventType": "STAY", "title": "시간을 보냄", "description": "{place}에서 시간을 보냈다.",
        "photos": [{"id": photo.clue_id, "activity": photo.activity or ""} for photo in base.photos],
        "evidence": [], "confidence": 0.7, "inferenceLevel": "EVIDENCE_BASED", "reason": "근거",
    }


def _run(monkeypatch, day: _Day, *, timeline: str, repair: list[str]):
    """runner 를 그대로 돌린다. Repair 가 확정할 때마다 받은 draft 와 App Server 가 받은 저장 요청을 돌려준다."""

    confirmed = []

    async def pipeline(request, *, on_confirm=None, **_kwargs):
        def record(draft):
            confirmed.append(draft)
            if on_confirm is not None:
                on_confirm(draft)

        interpretations = {
            "photos": [{"id": label, "activity": activity} for label, (_, activity) in day.photos.items()]
        }
        return await run_main_agent(
            request,
            timeline_agent=TimelineAgent(llm=FakeLLM([timeline])),
            repair_agent=RepairAgent(llm=FakeLLM(repair), max_iterations=_ITERATIONS),
            question_agent=silent_question_agent(),
            photo_clue_agent=PhotoClueAgent(
                llm=FakeLLM([json.dumps(interpretations, ensure_ascii=False)]), describer=_NoDescribe()
            ),
            notification_clue_agent=NotificationClueAgent(llm=FakeLLM([json.dumps({"clues": []})])),
            on_confirm=record,
        )

    monkeypatch.setattr(timeline_runner, "run_main_agent", pipeline)
    client = FakeAppServerClient(snapshot=_snapshot(day.request))
    status = asyncio.run(
        timeline_runner.process_timeline_task(
            _TASK_ID, client, 203, "2026-06-20T00:00:00+09:00", "2026-06-21T00:00:00+09:00", _TOKEN
        )
    )
    assert status is TaskStatus.SUCCESS
    [stored] = client.submit_calls
    return confirmed, stored.payload


# --- 구조 비교 -----------------------------------------------------------------------------------


_GROUPS = (_LOCATION, (EventSourceType.PHOTO,), (EventSourceType.CALENDAR,))


def _expected(day: _Day) -> list[tuple]:
    """코드의 기준: 기본 event 마다 (시간, 위치 기록, 사진, 일정)."""

    return sorted(
        (base.start, base.end, *day.split(ref.raw_id for ref in structural_refs(base)))
        for base in day.clues().base_events
    )


def _of_draft(day: _Day, draft) -> list[tuple]:
    return sorted((e.start_time, e.end_time, *day.split(r.raw_id for r in e.source_refs)) for e in draft.events)


def _of_storage(day: _Day, stored, events=None) -> list[tuple]:
    """저장 요청 body 의 구조."""

    events = stored.events if events is None else events
    return sorted((e.start_at, e.end_at, *day.split(e.source_raw_ids)) for e in events)


def _assert_kept(day: _Day, confirmed, stored) -> None:
    expected = _expected(day)
    assert len(confirmed) == _ITERATIONS + 1, "첫 확정과 Repair 차례마다의 확정"
    for number, draft in enumerate(confirmed, start=1):
        assert _of_draft(day, draft) == expected, f"{number}번째 확정"
    assert _of_storage(day, stored) == expected, "저장 요청"


def _owners(day: _Day, stored) -> dict[str, list[int]]:
    """사진 id → 그 사진을 근거로 가진 저장 event 의 위치(0부터)."""

    owners: dict[str, list[int]] = {label: [] for label in day.photos}
    labels = {day.photo_id(label): label for label in day.photos}
    for position, event in enumerate(stored.events):
        for raw_id in event.source_raw_ids:
            if raw_id in labels:
                owners[labels[raw_id]].append(position)
    return owners


def _event_ids(day: _Day) -> dict[str, str]:
    """기본 event id → 첫 확정이 준 clientEventId. 번호는 표시 순서대로 붙는다."""

    return {base.base_id: f"event-{number:03d}" for number, base in enumerate(day.clues().base_events, start=1)}


def _photo_ref(day: _Day, label: str) -> dict:
    return {"sourceType": "PHOTO", "rawId": day.photo_id(label)}


def _stay_ref(item) -> dict:
    return {"sourceType": "STAY", "rawId": item.raw_id}


# --- 운영 사례 ------------------------------------------------------------------------------------


def _bad_edits_on_two_visits(day: _Day) -> str:
    """운영 Repair 2~3회차가 한 편집과, v4 에서 같은 결과를 노리는 편집."""

    store, hall = _event_ids(day).values()
    pieces = [
        ("10:47", "11:52", "CULTURE", "백화점 구경"),
        ("11:52", "13:43", "STAY", "시간을 보냄"),
        ("13:43", "14:16", "CULTURE", "크리스마스 장식 구경"),
        ("14:16", "14:33", "STAY", "시간을 보냄"),
        ("14:33", "15:24", "MEAL", "망고 디저트를 먹음"),
        ("15:24", "15:24", "STAY", "시간을 보냄"),
    ]
    split = [
        {"startTime": _iso(start), "endTime": _iso(end), "eventType": kind, "title": title, "description": f"{title}."}
        for start, end, kind, title in pieces
    ]
    return _plan(
        # 8.3시간 체류를 관람으로 바꾼다 — 운영에서는 이것이 4시간 검토 기준을 불렀다.
        ("update_event", {"clientEventId": store, "fields": {"eventType": "CULTURE", "title": "백화점 구경"}}),
        ("split_event", {"clientEventId": store, "parts": split}),
        # 방문을 디저트만 말하는 조각으로 줄인다.
        (
            "update_event",
            {
                "clientEventId": store,
                "fields": {
                    "eventType": "MEAL", "title": "망고 디저트를 먹음", "description": "망고 디저트를 먹었다.",
                    "startTime": _iso("14:33"), "endTime": _iso("15:24"),
                    "photos": ["P3"], "evidence": ["P3"], "sourceRefs": [_photo_ref(day, "P3")],
                },
            },
        ),
        # 장식 사진을 다른 event 로 보낸다.
        (
            "update_event",
            {
                "clientEventId": hall,
                "fields": {
                    "description": "크리스마스 장식 앞에서 시간을 보냈다.", "photos": ["P1", "P2"],
                    "evidence": ["P1", "P2"],
                    "sourceRefs": [_photo_ref(day, "P1"), _photo_ref(day, "P2"), _photo_ref(day, "P4")],
                },
            },
        ),
        # 같은 공간의 뒷부분을 떼어 낸다.
        ("update_event", {"clientEventId": hall, "fields": {"endTime": _iso("16:00")}}),
        ("delete_event", {"clientEventId": hall}),
    )


def test_bad_repair_edits_do_not_reach_the_stored_result(monkeypatch):
    day = _two_visits_day()
    bad = _bad_edits_on_two_visits(day)

    confirmed, stored = _run(monkeypatch, day, timeline=_narrations(day, _plain), repair=[bad])

    _assert_kept(day, confirmed, stored)
    assert len(stored.events) == 2, "한 방문에서 갈라진 체류 카드가 없다."
    assert _owners(day, stored) == {"P1": [0], "P2": [0], "P3": [0], "P4": [1], "P5": [1]}, (
        "장식·디저트는 백화점 방문, 실내·입구는 음악감상실 방문에 한 번씩 있다."
    )
    first, second = stored.events
    assert (first.start_at, first.end_at) == (datetime(2026, 6, 20, 10, 47, tzinfo=_KST), datetime(2026, 6, 20, 15, 24, tzinfo=_KST))
    assert (second.start_at, second.end_at) == (datetime(2026, 6, 20, 15, 32, tzinfo=_KST), datetime(2026, 6, 20, 19, 8, tzinfo=_KST))
    # 편집은 실제로 실행됐다. 글과 종류는 Repair 의 것이고, 구조만 코드의 것이다.
    assert (first.event_type.value, first.title) == ("MEAL", "망고 디저트를 먹음")
    assert second.subtitle == "크리스마스 장식 앞에서 시간을 보냈다."


def test_a_timeline_that_splits_by_photo_is_stored_as_the_codes_visits(monkeypatch):
    """Timeline 이 사진마다 event 를 나눈 답을 내도 Repair·저장을 지나 방문 둘이다."""

    day = _two_visits_day()
    store, hall = day.clues().base_events
    split_answer = json.dumps(
        {
            "conversations": [],
            "events": [
                {**_plain(store), "title": "크리스마스 장식", "photos": [{"id": "P1", "activity": ""}]},
                {**_plain(store), "title": "망고 디저트를 먹음", "eventType": "MEAL", "photos": [{"id": "P3", "activity": ""}]},
                {**_plain(hall), "title": "실내", "photos": [{"id": "P4", "activity": ""}]},
                {**_plain(hall), "id": f"{hall.base_id}-2", "title": "입구", "photos": [{"id": "P5", "activity": ""}]},
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )

    confirmed, stored = _run(monkeypatch, day, timeline=split_answer, repair=[_bad_edits_on_two_visits(day)])

    _assert_kept(day, confirmed, stored)
    assert _owners(day, stored) == {"P1": [0], "P2": [0], "P3": [0], "P4": [1], "P5": [1]}


def test_a_real_revisit_stays_two_visits(monkeypatch):
    """같은 카페에 두 번 간 날은 저장까지 두 방문이다. 코드는 같은 장소라는 이유로 합치지 않는다."""

    day = _revisit_day()
    ids = list(_event_ids(day).values())
    noon, night = ids[0], ids[-1]
    cafe_noon, _, cafe_night = day.request.stays
    bad = _plan(
        ("update_event", {"clientEventId": noon, "fields": {"endTime": _iso("19:00"), "title": "카페에 두 번 감"}}),
        (
            "update_event",
            {
                "clientEventId": noon,
                "fields": {"sourceRefs": [_stay_ref(cafe_noon), _stay_ref(cafe_night), _photo_ref(day, "P1"), _photo_ref(day, "P2")]},
            },
        ),
        ("delete_event", {"clientEventId": night}),
    )

    confirmed, stored = _run(monkeypatch, day, timeline=_narrations(day, _plain), repair=[bad])

    _assert_kept(day, confirmed, stored)
    owners = _owners(day, stored)
    assert len(owners["P1"]) == len(owners["P2"]) == 1 and owners["P1"] != owners["P2"]
    cafe_events = [e for e in stored.events if {cafe_noon.raw_id, cafe_night.raw_id} & set(e.source_raw_ids)]
    assert len(cafe_events) == 2, "점심과 저녁의 카페 방문이 따로 남는다."


# --- 일정 ----------------------------------------------------------------------------------------


def _bad_edits_on_calendar_day(day: _Day) -> str:
    """회의에 방문의 시간·사진을 주고(방문과 회의 합치기), 일정을 바꿔 끼우고, 방문을 지운다."""

    ids = _event_ids(day)
    kinds = {
        (base.calendar.title if base.calendar is not None and base.segment is None else base.kind.value): ids[base.base_id]
        for base in day.clues().base_events
    }
    office, meeting, call = kinds["STAY"], kinds["주간 회의"], kinds["고객 통화"]
    [office_stay] = day.request.stays
    meeting_calendar = next(item for item in day.request.calendars if item.title == "주간 회의")
    return _plan(
        (
            "update_event",
            {
                "clientEventId": meeting,
                "fields": {
                    "eventType": "MEETING", "title": "주간 회의를 함", "description": "하루 종일 회의를 했다.",
                    "startTime": _iso("09:00"), "endTime": _iso("18:00"), "photos": ["P1"], "evidence": ["P1"],
                    "sourceRefs": [_stay_ref(office_stay), _photo_ref(day, "P1"), {"sourceType": "CALENDAR", "rawId": meeting_calendar.raw_id}],
                },
            },
        ),
        ("update_event", {"clientEventId": call, "fields": {"evidence": ["C1"], "sourceRefs": [{"sourceType": "CALENDAR", "rawId": meeting_calendar.raw_id}]}}),
        ("update_event", {"clientEventId": office, "fields": {"evidence": ["C1", "C2"]}}),
        ("ensure_calendar_events", {}),
        ("delete_event", {"clientEventId": office}),
    )


def test_calendar_events_keep_their_bounds_and_sources_through_repair_and_storage(monkeypatch):
    day = _calendar_day()

    confirmed, stored = _run(monkeypatch, day, timeline=_narrations(day, _plain), repair=[_bad_edits_on_calendar_day(day)])

    _assert_kept(day, confirmed, stored)
    calendar_ids = [item.raw_id for item in day.request.calendars]
    held = [raw_id for event in stored.events for raw_id in event.source_raw_ids if raw_id in calendar_ids]
    assert sorted(held) == sorted(calendar_ids), "일정마다 한 번씩이다 — 복제도 누락도 없다."
    owners = _owners(day, stored)
    [office_stay] = day.request.stays
    office = next(position for position, e in enumerate(stored.events) if office_stay.raw_id in e.source_raw_ids and not set(e.source_raw_ids) & set(calendar_ids))
    assert owners["P1"] == [office], "방문 사진은 시간이 겹치는 회의로 옮겨지지 않는다."
    meeting = next(e for e in stored.events if any(c.title == "주간 회의" and c.raw_id in e.source_raw_ids for c in day.request.calendars))
    assert (meeting.start_at.hour, meeting.end_at.hour) == (14, 15), "회의는 방문과 합쳐지지 않고 일정 시간이다."


def test_all_day_calendars_lead_the_stored_day_once_each(monkeypatch):
    day = _calendar_day()

    _, stored = _run(monkeypatch, day, timeline=_narrations(day, _plain), repair=[_bad_edits_on_calendar_day(day)])

    all_day = {item.raw_id for item in day.request.calendars if item.all_day}
    leading = [set(e.source_raw_ids) & all_day for e in stored.events[:2]]
    assert all(len(found) == 1 for found in leading) and set().union(*leading) == all_day
    assert not any(set(e.source_raw_ids) & all_day for e in stored.events[2:]), "종일 일정은 한 번씩만 나온다."
    starts = [e.start_at for e in stored.events[2:]]
    assert starts == sorted(starts), "그 뒤는 시간순이다."


def test_all_day_calendars_do_not_change_the_visits_or_photos(monkeypatch):
    with_all_day = _calendar_day()
    without = _calendar_day(all_day=False)

    _, stored_with = _run(monkeypatch, with_all_day, timeline=_narrations(with_all_day, _plain), repair=[_bad_edits_on_calendar_day(with_all_day)])
    _, stored_without = _run(monkeypatch, without, timeline=_narrations(without, _plain), repair=[_bad_edits_on_calendar_day(without)])

    all_day = {item.raw_id for item in with_all_day.request.calendars if item.all_day}
    rest = [e for e in stored_with.events if not set(e.source_raw_ids) & all_day]
    assert _of_storage(with_all_day, stored_with, rest) == _of_storage(without, stored_without)


# --- 저장 직전 자리 ------------------------------------------------------------------------------


def _confirmed(day: _Day) -> tuple[TimelineDraft, object]:
    """글이 하나도 없는 draft 를 확정한다. 하루가 틀 글로 채워지고 되살림 warning 이 남는다."""

    clues = day.clues()
    draft = TimelineDraft(user_id="u", date="2026-06-20", timezone="Asia/Seoul", events=[])
    repair_draft(draft, day.request, clues=clues, issued_ids=set())
    return draft, clues


def test_storage_brings_back_the_codes_structure_if_something_broke_it_after_the_confirm(caplog):
    day = _two_visits_day()
    draft, clues = _confirmed(day)
    store, hall = draft.events
    # 확정 뒤에 구조를 바꾸는 단계가 생겼다고 치자: 장식 사진이 옆 방문으로, 방문 시간이 잘리고.
    hall.source_refs.append(SourceRef(source_type=EventSourceType.PHOTO, raw_id=day.photo_id("P1")))
    store.end_time = datetime(2026, 6, 20, 14, 16, tzinfo=_KST)

    enforce_final_photo_assignment(draft, day.request, prefer_extracted=True, plan=clues)

    assert _of_draft(day, draft) == _expected(day)
    assert [e.client_event_id for e in draft.events] == ["event-001", "event-002"], "번호는 그대로다."
    assert "코드가 정한 구조와 달라" in caplog.text


def test_storage_leaves_a_confirmed_draft_as_it_is():
    day = _two_visits_day()
    draft, clues = _confirmed(day)
    before = draft.model_dump(by_alias=True, mode="json")

    enforce_final_photo_assignment(draft, day.request, prefer_extracted=True, plan=clues)

    assert draft.model_dump(by_alias=True, mode="json") == before, "마지막 확정의 되살림 warning 도 그대로다."


def test_a_photo_event_made_right_before_storage_keeps_all_day_calendars_first(monkeypatch):
    """촬영 시각을 못 읽은 사진은 코드가 정한 event 에 들지 않고 저장 직전에 사진 event 가 된다. 그래도 종일 일정이
    맨 앞이다 — 계획 없이 다시 정렬하자 종일 일정이 맨 뒤로 갔다."""

    day = _calendar_day(unreadable_photo=True)

    _, stored = _run(monkeypatch, day, timeline=_narrations(day, _plain), repair=[_bad_edits_on_calendar_day(day)])

    all_day = {item.raw_id for item in day.request.calendars if item.all_day}
    assert [bool(set(e.source_raw_ids) & all_day) for e in stored.events[:2]] == [True, True]
    assert len(_owners(day, stored)["P3"]) == 1, "그 사진도 한 번은 저장된다."
