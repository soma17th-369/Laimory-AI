"""v4 프롬프트 계약 (#167~#171).

내용의 좋고 나쁨은 live 비교가 잰다. 여기서는 코드와 프롬프트가 같은 것을 말하는지 본다.
입력 키, 검토 기준 값, 문장 형식 표, User Memory 필드 정의, 코드가 남기는 검사 종류가 그것이다.
한쪽만 바꾸면 모델은 없는 키를 찾고 있는 키는 모른다.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents.events.notification.clue_agent import build_notification_clue_payload
from app.schemas import EventType, GeoPlace, MovementItem, NotificationItem
from app.schemas.clues import NotificationClue, NotificationClueKind, PhotoClue
from app.schemas.user_memory import NARRATIVE_FIELDS
from app.services.calendar_clues import build_calendar_clues
from app.services.clue_bundles import build_timeline_clues, timeline_input_sections
from app.services.duration_guard import DURATION_LIMITS
from app.services.event_count_guard import MAX_EVENT_COUNT
from app.services.location_skeleton import build_location_skeleton
from tests.fixtures.requests import calendar_item, fixture_raw_id, make_request, stay_item

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
_KST = timezone(timedelta(hours=9))


def _read(path: str) -> str:
    return (APP_ROOT / path).read_text(encoding="utf-8")


def _timeline() -> str:
    return _read("agents/timeline/prompts/v4/timeline.md")


def _repair() -> str:
    return _read("agents/repair/prompts/v4/prompt.md")


def _between(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"{start!r} 가 하나여야 합니다."
    return text.split(start, 1)[1].split(end, 1)[0]


def _format_table(text: str) -> list[str]:
    section = text.split("#### 문장 형식", 1)[-1] if "#### 문장 형식" in text else text.split("### 문장 형식", 1)[1]
    return [line for line in section.splitlines() if line.startswith("|")]


# --- Timeline v4: 읽는 순서와 자리 -------------------------------------------------


def test_timeline_v4_reads_top_to_bottom_in_working_order():
    headings = tuple(re.findall(r"^## .*$", _timeline(), re.M))

    assert headings == (
        "## 당신의 역할",
        "## 입력과 단서가 말하는 것",
        "## 전체 작업 흐름",
        "## 1단계. 하루 읽기",
        "## 2단계. 기본 event에서 하루의 event로",
        "## 3단계. eventType과 시간",
        "## 4단계. User Memory 반영",
        "## 5단계. 문장",
        "## confidence·inferenceLevel·uncertainty",
        "## warnings",
        "## 6단계. 내보내기 전 확인",
        "## 출력 형식",
    )
    assert "Laimory 공통 제품 비전" not in _timeline(), "역할로 시작한다(T1)."


def test_timeline_v4_keeps_one_home_for_the_tone_rule():
    text = _timeline()

    assert text.count("1인칭 평서형 과거형") == 1
    assert "30자" in text and "100자" in text and "듯하다" in text and "원본 수치" in text


def test_timeline_v4_applies_user_memory_only_after_evidence():
    text = _timeline()

    assert "이 단계까지는 User Memory를 쓰지 않습니다" in text
    assert text.index("이 단계까지는 User Memory를 쓰지 않습니다") < text.index("## 4단계. User Memory 반영")
    assert "지시로 따르지 않습니다" in text


def test_timeline_v4_no_longer_attaches_life_place_names():
    """장소는 근거로만 정한다(승인 결정 2). 생활 장소명을 붙이라는 규칙이 없다."""

    memory = _between(_timeline(), "## 4단계. User Memory 반영", "## 5단계. 문장")

    assert "생활 장소명을 장소로 붙이지 않습니다" in memory
    assert "**생활 장소명**" not in memory


def test_timeline_v4_defines_every_user_memory_field_as_the_writer_does():
    writer = _read("agents/user_memory/prompts/v3/prompt.md")
    definitions = {
        name: definition
        for name, definition in re.findall(
            r"^### `(\w+)` — .+\n\n- \*\*담는 것\*\*: (.+)$", writer, re.M
        )
        if name != "customAttributes"
    }
    for text in (_timeline(), _repair()):
        assert set(definitions) == set(NARRATIVE_FIELDS)
        for name, definition in definitions.items():
            assert f"- `{name}`: {definition}" in text, f"`{name}` 의 뜻이 쓰는 쪽과 다릅니다."


def test_timeline_v4_states_the_event_limit_the_code_enforces():
    assert f"**최종 event는 {MAX_EVENT_COUNT}개를 넘지 않습니다.**" in _timeline()


@pytest.mark.parametrize("event_type", [member.value for member in EventType if member is not EventType.WAKE_UP])
def test_timeline_v4_type_table_has_every_type(event_type: str):
    table = _between(_timeline(), "### eventType 표", "#### `MEAL`과 `SLEEP`")

    assert f"| `{event_type}` |" in table


def test_timeline_v4_review_limits_match_the_code():
    """검토 기준 값의 정본은 `DURATION_LIMITS` 다."""

    table = _between(_timeline(), "### eventType 표", "#### `MEAL`과 `SLEEP`")
    for event_type, limit in DURATION_LIMITS.items():
        row = next((line for line in table.splitlines() if line.startswith(f"| `{event_type.value}` |")), None)
        if limit is None:
            if row is not None:
                assert "검토 기준" not in row, f"{event_type.value} 는 검토 기준이 없습니다."
            continue
        hours = int(limit.total_seconds() // 3600)
        assert row is not None and f"검토 기준 {hours}시간" in row, event_type.value


# --- 장소 칸과 문장 ----------------------------------------------------------------


def test_v4_prompts_write_the_place_as_the_slot_only():
    for text in (_timeline(), _repair()):
        assert "{place}" in text
        for gone in ("{장소}", "{도착지}", "{출발지}", "{가맹점}"):
            assert gone not in text, gone
        assert "`에서`·`에`·`의`·`까지`" in text, "칸 뒤의 조사는 받침과 무관한 것만이다(승인 결정 1)."


def test_v4_titles_never_carry_a_place_name():
    # 장소명 대신 같은 장소 묶음마다 코드가 고른 지명(`area`)을 Timeline 이 그대로 쓴다(사용자 결정 2026-10-29).
    for text in (_timeline(), _repair()):
        assert "`title`에는 장소 이름을 쓰지 않고, 근거의 `area`로 어디였는지 보탭니다" in text
        assert "{동네}" not in text
    for row in _format_table(_timeline())[2:]:
        title = row.split(" | ")[1]
        assert "{place}" not in title, row


def test_v4_movement_title_and_sentence_use_the_same_route():
    """이동 제목과 문장은 LLM 이 같은 출발·도착 지명으로 쓴다(사용자 결정 2026-10-30).

    "title 이랑 description 이 안 맞으면 안되잖아". 제목은 Timeline, 문장은 코드가 따로 쓰자
    기록되지 않은 열차 구간 때문에 둘의 출발지가 갈렸다.
    """

    for text in (_timeline(), _repair()):
        movement = next(row for row in _format_table(text) if row.startswith("| 이동 |"))
        assert "`{출발 지명}에서 {도착 지명}으로 {목적}했다.` 제목과 같은 지명이다" in movement
        assert "코드가" not in movement
        assert "`from`·`to`의 `area`" in text


def test_timeline_v4_follows_the_base_events_without_being_told_to_merge():
    """Timeline 은 기본 event 의 틀을 따르고, 합치라는 말을 듣지 않는다(사용자 결정 2026-10-30).

    "합치지 말라고 하는 게 아니라 그냥 저 틀을 따라라 라고 하면서 합치라는 말을 아예 안하는 거지".
    합치기를 허용했을 때 장소 이름 후보만 보고 다른 장소를 한 event 로 묶을 수 있었다.
    """

    text = _timeline()
    assert "기본 event 하나가 하루의 event 하나" in text
    assert not re.search(r"합치|합쳐|합친|병합", text)


def test_repair_v4_tells_places_apart_by_the_code_address():
    """다른 장소인지는 코드가 같은 장소마다 하나로 맞춘 주소로 가른다 — 장소 이름은 후보라 기준이 못 된다."""

    text = _repair()
    assert "주소가 다른 체류 구간을 한 event가 함께 가지면 이유와 상관없이 장소마다 나눕니다" in text
    assert "어느 조각으로도 채우지 않습니다" in text


def test_timeline_and_repair_v4_share_one_sentence_table():
    timeline = _format_table(_timeline())
    repair = _format_table(_repair())

    assert timeline == repair
    assert timeline[0] == "| 상황 | `title` | `description` | 예 |"
    for row in timeline[3:]:
        description = row.split(" | ")[2]
        assert "다.`" in description, row


def test_timeline_v4_picks_candidates_from_the_pool_only():
    section = _between(_timeline(), "### 장소 후보", "### 문장 형식")

    assert "최대 5개" in section
    assert "목록에 없는 이름을 만들거나 고쳐 쓰지 않습니다" in section
    assert "근거에 장소 이름이 있으면 모든 event에 후보를 줍니다" in section, "장소명은 언제나 후보 목록이다(사용자 결정)."
    assert "빈 목록은 근거에 장소 이름이 하나도 없을 때뿐입니다" in section


# --- 입력 키 -------------------------------------------------------------------------


def _all_keys(value, keys: set[str]) -> set[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.add(key)
            _all_keys(inner, keys)
    elif isinstance(value, list):
        for inner in value:
            _all_keys(inner, keys)
    return keys


def _rich_clues():
    office = stay_item("office", lat=37.6, start="2026-06-20T09:00:00", end="2026-06-20T12:00:00", places=["OO타워"])
    corner = stay_item("corner", lat=37.6004, start="2026-06-20T12:05:00", end="2026-06-20T18:00:00", places=["OO식당"])
    far = stay_item("far", lat=37.9, start="2026-06-20T20:00:00", end="2026-06-20T21:00:00")
    trip = calendar_item("trip", "출장", start="2026-06-20T00:00:00", end="2026-06-20T23:00:00")
    meeting = calendar_item("m", "회의", start="2026-06-20T10:00:00", end="2026-06-20T11:00:00", location_text="본사(서울 강남구 테헤란로 152)")
    commute = MovementItem(
        raw_id=fixture_raw_id("movement-commute"), start_at="2026-06-20T08:00:00", end_at="2026-06-20T09:00:00",
        start=GeoPlace(latitude=37.5, longitude=127.0, address="출발 주소", places=["집 근처"]),
        end=GeoPlace(latitude=37.6, longitude=127.0, address="도착 주소", places=["OO타워"]),
        transports=["IN_VEHICLE"], distance_meters=12000,
    )
    loose = stay_item("loose", lat=38.5, start="2026-06-20T22:30:00", end="2026-06-20T22:40:00")
    request = make_request(stays=[office, corner, far, loose], movements=[commute], calendars=[trip, meeting])
    photo = PhotoClue(
        clue_id="P1", raw_id=fixture_raw_id("photo-1"), taken_at=datetime(2026, 6, 20, 12, 30, tzinfo=_KST),
        places=["OO식당"], address="주소", latitude=37.6, longitude=127.0, description="식탁", activity="먹었다",
        uncertainty=["동행"],
    )
    chat = NotificationClue(
        clue_id="N1", kind=NotificationClueKind.CONVERSATION, raw_ids=[fixture_raw_id("n-1")],
        app_names=["카카오톡"], counterpart="김민수", summary="약속",
        first_at=datetime(2026, 6, 20, 10, 0, tzinfo=_KST), last_at=datetime(2026, 6, 20, 10, 5, tzinfo=_KST),
    )
    news = NotificationClue(
        clue_id="N2", kind=NotificationClueKind.RESERVATION, raw_ids=[fixture_raw_id("n-2")],
        counterpart="OO헤어", summary="예약", schedule_today=False,
    )
    return build_timeline_clues(build_location_skeleton(request), build_calendar_clues(request), [photo], [chat, news])


def test_timeline_v4_names_every_input_key_the_code_sends():
    sections = timeline_input_sections(_rich_clues())
    # 최상위 키는 `[하루]`·`[근거 묶음]` 같은 구역 제목으로 실린다.
    keys = _all_keys(list(sections.values()), set())
    text = _timeline()

    for key in sorted(keys):
        assert f"`{key}`" in text, f"timeline v4 에 입력 키 `{key}` 설명이 없습니다."


def test_notification_v4_names_every_payload_key():
    def item(label: str, app_name: str, title: str) -> NotificationItem:
        return NotificationItem(
            rawId=fixture_raw_id(label), postedAt="2026-06-20T09:00:00+09:00",
            appName=app_name, title=title, text="본문",
        )

    payload = build_notification_clue_payload(
        [item("pay", "토스", "결제"), item("chat", "카카오톡", "김민수"), item("etc", "자리톡", "안내")]
    ).body
    keys = _all_keys(payload, set())
    text = _read("agents/events/notification/prompts/v4/prompt.md")

    for key in sorted(keys):
        assert f"`{key}`" in text, f"notification v4 에 입력 키 `{key}` 설명이 없습니다."
    assert "`rawId`" not in text, "v4 알림 입력은 rawId 대신 짧은 id 를 싣는다."
    for kind in NotificationClueKind:
        if kind is not NotificationClueKind.PROMOTION:  # 스키마에만 남은 값이다(#173).
            assert kind.value in text


def test_photo_v4_reads_what_the_user_did_one_photo_at_a_time():
    text = _read("agents/events/photo/prompts/v4/prompt.md")

    assert "사용자가 그때 한 일" in text
    for key in ("id", "takenAt", "places", "address", "description", "activity", "uncertainty"):
        assert f"`{key}`" in text or f'"{key}"' in text, key
    assert "candidate" not in text and "eventType" not in text, "사진 단서는 사건을 정하지 않는다."


# --- Repair v4 --------------------------------------------------------------------


def test_repair_v4_explains_every_finding_the_v4_confirm_pass_reports():
    text = _repair()

    for kind in (
        "LONG_STAY_BETWEEN_MOVEMENTS",
        "DURATION_OVER_LIMIT",
        "UNSUPPORTED_MEAL",
        "STAY_WORD_IN_TITLE",
        "TIME_IN_TITLE",
        "PLACE_NAME_IN_TITLE",
        "SAME_TITLE_IN_A_ROW",
        "EVENT_TO_WRITE",
        "TEXT_CORRECTION",
    ):
        assert f"`{kind}`" in text, kind
    # `LOCATION_ONLY_OVERLAP` 은 v4 확정이 내지 않는다(사용자 결정 2026-10-31) — 받은 Repair 가 7시간 체류를 식사로 고쳐 썼다.
    for gone in ("ADDRESS_IN_NARRATION", "PLACE_MISMATCH", "PLACE_NAME_MISMATCH", "LOCATION_ONLY_OVERLAP", "rerun_event_agent", "장소 후보 다시 고르기"):
        assert gone not in text, gone


def test_repair_v4_reads_the_merge_reason_and_clue_ids():
    text = _repair()

    assert "`mergeReason`" in text
    assert "`unusedClues`" in text
    assert "`evidence`" in text


def test_repair_v4_keeps_the_v3_tone_contract():
    text = _repair()

    assert "1인칭 평서형 과거형" in text
    assert "VERBOSE_NARRATION" in text
    assert "헤지" in text


def test_v4_calendar_owns_its_time_and_a_stay_without_activity_is_filled():
    """일정은 그 시간의 주인이고 위치는 그 근거다. 활동을 모르는 체류는 사진으로 채운다(사용자 결정 2026-10-30).

    "캘린더 일정은 중요한 일정이라서 오히려 위치가 그것의 근거가 되어야돼", "보낸 시간은 사실 어떤
    활동이 아니잖아 그래서 함께한 순간이랑 따로 볼 수가 없는거야". 실제 재실행에서 회의 일정과 같은
    장소의 `보낸 시간` 이 따로 나왔고, 두 사람 사진 한 장이 `함께한 시간` 으로 꺼내졌다. 「꺼내기」의
    `(식사·회의·수업·별도 방문)` 은 v3 의 "긴 체류 안의 짧은 사건" 에서 왔는데 일부라는 조건이 빠졌었다.
    """

    timeline = _timeline()
    assert "**일정은 그 시간의 주인이고 위치는 그 근거입니다.**" in timeline
    assert "**활동을 모르는 체류는 다른 활동과 견줄 수 없습니다.**" in timeline
    assert "식사·회의·수업·별도 방문" not in timeline
    assert "그 시간의 일부에서 끝나는 다른 사건" in timeline
    repair = _repair()
    assert "식사·회의·수업·별도 방문" not in repair
    assert "일정이 event 시간의 대부분을 덮으면 그 일정이 event의 주인이고 위치는 그 근거라 나누지 않습니다." in repair


def test_v4_evidence_must_match_both_time_and_place():
    """근거는 시간과 장소를 둘 다 본다 — 사진만이 아니라 모든 근거(사용자 결정 2026-10-30).

    같은 곳(일청담)에 아침과 점심 두 번 간 날, Timeline 이 12:49 점심 사진을 아침 체류의 근거로 적었고
    Repair 가 그걸 믿고 아침 체류를 식사로 바꿨다. "같은 위치에서 찍은 사진은 다른 묶음에서 가져온다" 는
    문장이 장소만으로 가져오게 하고 시간을 깎아내렸다.
    """

    timeline, repair = _timeline(), _repair()
    assert "**근거는 시간과 장소가 모두 맞는 event에만 씁니다.**" in timeline
    assert "같은 위치에서 찍은 사진" not in timeline
    assert "**근거는 단서의 시간과 장소가 모두 그 event와 맞을 때만 넣습니다**" in repair
    assert "의미상 다른 event의 것이면" not in repair
    for text in (timeline, repair):
        assert "장소 이름이 같아도 시간이 다르면 다른 방문입니다" in text



def test_timeline_v4_makes_events_from_notifications_on_a_day_without_location():
    """위치 없는 날 결제·대화를 모두 버려 v3 절반의 event 가 됐다. 그날은 알림 자체가 event 다."""

    text = _timeline()

    assert "다르면 내용이 있는 대화만 그날의 event이고" in text
    assert "대화가 아닌 알림은 event가 되지 않습니다" in text
    assert "구간에 붙지 않은 사진·일정, 대화" in text


def test_timeline_v4_uses_notifications_as_evidence_not_events():
    """알림은 틀의 근거다(#173). `scheduleToday` 규칙이 결제까지 막지 않는다."""

    text = _timeline()

    assert "**대화가 아닌 알림은 event를 새로 만들거나 꺼내지 않습니다.**" in text
    assert "다만 알림은 담기지 않아 `N5`처럼 직접 적습니다" in text
    assert "`scheduleToday`가 `true`가 아닌 예약·일정 알림" in text
    assert "`PROMOTION`" not in text and "`unread`" not in text


def test_notification_v4_keeps_only_what_is_needed():
    """필요한 알림만 남기고 나머지는 `dropped` 에 id 만 적는다(#173)."""

    text = _read("agents/events/notification/prompts/v4/prompt.md")

    assert "들지 않거나 판단이 서지 않으면 버립니다" in text
    assert "`dropped`" in text and "버리는 알림에는 요약을 쓰지 않습니다" in text
    assert "PROMOTION" not in text


def test_timeline_v4_keeps_what_the_photo_shows_in_the_meal_sentence():
    """Timeline 식사 문장 32건 중 21건이 `음식을 먹었다` 였다. 뭉개지 말라고만 하자 없는 음식을 지어냈다."""

    text = _timeline()

    assert "말하는 음식은 그 말대로 쓰고, 말하지 않은 음식은 지어내지 않습니다" in text
    assert "음식을 모르면 `{지명}에서 {끼니}`" in text


def test_timeline_v4_takes_a_meal_out_of_a_meeting():
    """일정의 타입을 Timeline 이 정하자 음식 사진을 회의에 흡수해 식사가 사라졌다(사용자 결정: 둘 다 산다)."""

    text = _timeline()

    assert "만남·회의·일정 중이어도 사진의 `activity`가 먹었다고 말하거나 음식 결제가 있으면 `MEAL`로" in text
    assert "회의 장면 사진" in text



def test_timeline_v4_keeps_a_calendar_a_calendar():
    """캘린더라면 캘린더 일정으로 저장한다(사용자 결정 2026-10-31). 수면 일정만 `SLEEP` 이다."""

    text = _timeline()

    assert "캘린더 일정은 `CALENDAR_EVENT`로 남깁니다" in text
    assert "회의는 `MEETING`" not in text
    assert "수면 일정은 `SLEEP`입니다" in text
