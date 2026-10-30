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


def test_v4_movement_sentence_is_written_by_the_code():
    rows = _format_table(_timeline())
    movement = next(row for row in rows if row.startswith("| 이동 |"))

    assert "코드가 출발·도착 지명(구 + 동)으로 쓴다" in movement, "이동 문장은 주소 원문이 아니라 지명이다(사용자 결정)."


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
    unread = NotificationClue(clue_id="N3", raw_ids=[fixture_raw_id("n-3")], app_names=["자리톡"])
    return build_timeline_clues(build_location_skeleton(request), build_calendar_clues(request), [photo], [chat, news, unread])


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
        "LOCATION_ONLY_OVERLAP",
        "UNSUPPORTED_MEAL",
        "STAY_WORD_IN_TITLE",
        "TIME_IN_TITLE",
        "PLACE_NAME_IN_TITLE",
    ):
        assert f"`{kind}`" in text, kind
    for gone in ("ADDRESS_IN_NARRATION", "PLACE_MISMATCH", "PLACE_NAME_MISMATCH", "rerun_event_agent", "장소 후보 다시 고르기"):
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
