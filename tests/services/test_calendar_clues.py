"""일정 단서 검증 (#168). 테스트 이름 앞의 `C번호` 는 계획의 「일정 규칙 이식표」 줄이다."""

from app.services.calendar_clues import MEMO_MAX_LENGTH, build_calendar_clues
from tests.fixtures.requests import calendar_item, make_request


def _clues(*items):
    return build_calendar_clues(make_request(calendars=list(items)))


def test_C1_every_calendar_item_becomes_a_clue_in_time_order():
    late = calendar_item("late", "저녁 약속", start="2026-06-20T19:00:00", end="2026-06-20T21:00:00")
    early = calendar_item("early", "팀 회의", start="2026-06-20T10:00:00", end="2026-06-20T11:00:00")
    broken = calendar_item("broken", "시각 없는 일정", start="시각 아님", end=None)

    clues = _clues(late, early, broken)

    assert [clue.title for clue in clues] == ["팀 회의", "저녁 약속", "시각 없는 일정"]
    assert [clue.clue_id for clue in clues] == ["C1", "C2", "C3"]
    assert clues[2].start is None
    assert clues[2].as_prompt_dict()["untimed"] is True


def test_C2_title_and_time_are_carried_as_written():
    [clue] = _clues(
        calendar_item("a", "  프로젝트 회의  ", start="2026-06-20T14:00:00", end="2026-06-20T15:30:00")
    )

    payload = clue.as_prompt_dict()
    assert payload["title"] == "프로젝트 회의"
    assert payload["startAt"].startswith("2026-06-20T14:00:00")
    assert payload["endAt"].startswith("2026-06-20T15:30:00")


def test_C4_place_and_address_are_split():
    [with_both] = _clues(calendar_item("a", "집들이", location_text="집(경기도 오산시 운암로 90)"))
    [address_only] = _clues(calendar_item("b", "미팅", location_text="서울 강남구 테헤란로 152"))
    [name_only] = _clues(calendar_item("c", "회의", location_text="3층 대회의실"))

    assert (with_both.place, with_both.address) == ("집", "경기도 오산시 운암로 90")
    assert (address_only.place, address_only.address) == (None, "서울 강남구 테헤란로 152")
    assert (name_only.place, name_only.address) == ("3층 대회의실", None)


def test_C4_parentheses_without_an_address_stay_part_of_the_name():
    [clue] = _clues(calendar_item("a", "회의", location_text="본관(3층)"))

    assert (clue.place, clue.address) == ("본관(3층)", None)


def test_C5_C6_all_day_item_is_a_background_clue():
    item = calendar_item("a", "워크숍", start="2026-06-20T00:00:00", end="2026-06-21T00:00:00")
    item.all_day = True

    [clue] = _clues(item)

    assert clue.all_day and clue.background
    assert not clue.multi_day, "다음 날 0시에 끝나는 종일 일정은 하루짜리다."


def test_C6_twelve_hours_or_more_is_background_and_less_is_not():
    [long_one] = _clues(calendar_item("a", "출장", start="2026-06-20T08:00:00", end="2026-06-20T20:00:00"))
    [shorter] = _clues(calendar_item("b", "근무", start="2026-06-20T09:00:00", end="2026-06-20T20:59:00"))

    assert long_one.background
    assert not shorter.background


def test_C7_multi_day_item_is_clipped_to_the_window_and_keeps_its_original_span():
    [clue] = _clues(
        calendar_item("trip", "제주 여행", start="2026-06-19T09:00:00", end="2026-06-22T18:00:00")
    )

    assert clue.multi_day and clue.clipped
    assert (clue.start.day, clue.start.hour) == (20, 0)
    assert (clue.end.day, clue.end.hour) == (21, 0)
    payload = clue.as_prompt_dict()
    assert payload["originalStartAt"].startswith("2026-06-19T09:00:00")
    assert payload["originalEndAt"].startswith("2026-06-22T18:00:00")


def test_C7_item_entirely_outside_the_window_is_kept_at_the_nearest_boundary():
    [before, after] = _clues(
        calendar_item("before", "어제 일정", start="2026-06-19T09:00:00", end="2026-06-19T10:00:00"),
        calendar_item("after", "내일 일정", start="2026-06-22T09:00:00", end="2026-06-22T10:00:00"),
    )

    assert before.start == before.end and before.start.day == 20
    assert after.start == after.end and after.start.day == 21


def test_C8_naive_times_are_marked_as_timezone_defaulted():
    [naive] = _clues(calendar_item("a", "회의", start="2026-06-20T10:00:00", end="2026-06-20T11:00:00"))
    [aware] = _clues(
        calendar_item("b", "회의", start="2026-06-20T10:00:00+09:00", end="2026-06-20T11:00:00+09:00")
    )

    assert naive.timezone_defaulted
    assert not aware.timezone_defaulted
    assert "timezoneDefaulted" not in aware.as_prompt_dict()


def test_C11_memo_is_cut_and_not_rewritten():
    item = calendar_item("a", "회의")
    item.description = "가" * 300

    [clue] = _clues(item)

    assert clue.memo == "가" * MEMO_MAX_LENGTH


def test_prompt_dict_leaves_out_false_flags_and_empty_values():
    [clue] = _clues(
        calendar_item("a", "회의", start="2026-06-20T10:00:00+09:00", end="2026-06-20T11:00:00+09:00")
    )

    assert set(clue.as_prompt_dict()) == {"id", "title", "startAt", "endAt"}


def test_no_calendar_items_give_no_clues():
    assert _clues() == []
