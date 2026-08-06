"""User Memory 갱신의 크기 정책 (#64).

여기서 지키는 두 가지가 계약이다.

- 입력은 **거절하지 않고 자른다.** 자를 때 메모 있는 event 를 끝까지 남긴다.
- 출력은 **자르지 않고 지적한다.** 압축은 의미 판단이라 코드가 문장을 건드리지 않는다.
"""

import pytest

from app.schemas.user_memory import UserMemory
from app.schemas.user_memory_update import DiaryEntry
from app.services.user_memory_limits import (
    MAX_DIARY_COUNT,
    MAX_EVENT_COUNT,
    MEMO_MAX_CHARS,
    TEXT_MAX_CHARS,
    USER_MEMORY_MAX_CHARS,
    build_diary_digest,
    find_violations,
    serialized_chars,
)
from tests.fixtures.user_memory import diary, diary_event


def _entries(payload: list[dict]) -> list[DiaryEntry]:
    return [DiaryEntry.model_validate(item) for item in payload]


# --- 입력 잘라내기 -----------------------------------------------------


def test_digest_keeps_only_the_most_recent_diaries():
    payload = [diary(date=f"2026-07-{day:02d}") for day in range(1, MAX_DIARY_COUNT + 4)]

    digest = build_diary_digest(_entries(payload))

    assert digest.stats["diaryCount"] == MAX_DIARY_COUNT
    assert digest.stats["droppedDiaryCount"] == 3
    # 남은 것은 최근 날짜이고, 프롬프트에는 오름차순으로 실린다.
    dates = [entry["date"] for entry in digest.diaries]
    assert dates == sorted(dates)
    assert dates[-1] == f"2026-07-{MAX_DIARY_COUNT + 3:02d}"


def test_digest_keeps_memo_events_when_over_budget():
    """메모는 성향 계열 필드의 유일한 근거다. 마지막까지 지킨다."""

    events = [
        diary_event(
            title=f"이벤트 {index}",
            start_at=f"2026-08-04T{index % 24:02d}:00:00+09:00",
            end_at=None,
        )
        for index in range(MAX_EVENT_COUNT + 10)
    ]
    # 가장 오래된 자리에 메모를 둔다. 시간 순으로만 자르면 이것부터 사라진다.
    events[0]["memo"] = "오늘은 오랜만에 마음이 놓였어요."
    events[0]["startAt"] = "2026-08-04T00:00:00+09:00"

    digest = build_diary_digest(_entries([diary(events=events)]))

    memos = [
        event.get("memo")
        for entry in digest.diaries
        for event in entry["events"]
        if event.get("memo")
    ]
    assert digest.stats["eventCount"] == MAX_EVENT_COUNT
    assert digest.stats["droppedEventCount"] == 10
    assert memos == ["오늘은 오랜만에 마음이 놓였어요."]


def test_digest_reports_memo_count_even_for_dropped_events():
    """센 것은 접수한 전부다. 자른 뒤 숫자만 보면 "메모 없는 날" 로 오해한다."""

    events = [
        diary_event(start_at=f"2026-08-04T{index % 24:02d}:30:00+09:00", end_at=None)
        for index in range(MAX_EVENT_COUNT + 5)
    ]
    for event in events:
        event["memo"] = "메모"

    digest = build_diary_digest(_entries([diary(events=events)]))

    assert digest.stats["memoCount"] == MAX_EVENT_COUNT + 5
    assert digest.has_memo


def test_digest_reports_no_memo_day():
    digest = build_diary_digest(_entries([diary()]))

    assert digest.stats["memoCount"] == 0
    assert not digest.has_memo


def test_digest_drops_the_minute_from_event_times():
    """분 단위 시각은 갱신 판단에 쓸모가 없고 프로필 문장에 샐 위험만 만든다."""

    digest = build_diary_digest(
        _entries([diary(events=[diary_event(start_at="2026-08-04T12:43:00+09:00")])])
    )

    event = digest.diaries[0]["events"][0]
    assert event["hour"] == 12
    assert "startAt" not in event
    assert "43" not in str(event)


def test_digest_omits_question_and_empty_values():
    """`question` 도 AI 가 쓴 문장이라 갱신 근거로 주지 않는다."""

    digest = build_diary_digest(
        _entries(
            [
                diary(
                    events=[
                        diary_event(
                            subtitle=None,
                            question="어떤 이야기가 기억에 남았나요?",
                            memo="  ",
                        )
                    ]
                )
            ]
        )
    )

    event = digest.diaries[0]["events"][0]
    assert "question" not in event
    assert "subtitle" not in event
    assert "memo" not in event


def test_digest_clips_long_text_instead_of_rejecting():
    digest = build_diary_digest(
        _entries(
            [
                diary(
                    events=[
                        diary_event(title="가" * 400, memo="나" * (MEMO_MAX_CHARS + 200))
                    ]
                )
            ]
        )
    )

    event = digest.diaries[0]["events"][0]
    assert len(event["title"]) == TEXT_MAX_CHARS
    assert len(event["memo"]) == MEMO_MAX_CHARS


def test_digest_skips_a_day_whose_events_were_all_dropped():
    """event 가 하나도 안 남은 날은 싣지 않는다. 모델이 "아무 일도 없던 날" 로 읽는다."""

    digest = build_diary_digest(_entries([diary(date="2026-08-03", events=[]), diary()]))

    assert [entry["date"] for entry in digest.diaries] == ["2026-08-04"]


def test_digest_of_nothing_is_empty_not_an_error():
    digest = build_diary_digest([])

    assert digest.diaries == []
    assert digest.stats["eventCount"] == 0


# --- 출력 검사 ---------------------------------------------------------


def test_clean_memory_has_no_violations():
    memory = UserMemory(basic_profile="30대 개발자입니다.")

    assert find_violations(memory) == []


def test_oversized_memory_is_reported_without_being_cut():
    memory = UserMemory(
        **{
            field: "가" * 200
            for field in (
                "basic_profile",
                "life_context",
                "relationships",
                "personality",
                "values",
                "preferences",
                "routines",
            )
        }
    )

    violations = find_violations(memory)

    assert serialized_chars(memory) > USER_MEMORY_MAX_CHARS
    assert len(violations) == 1
    assert str(USER_MEMORY_MAX_CHARS) in violations[0]
    # 지적했을 뿐 문장은 그대로다.
    assert len(memory.basic_profile) == 200


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("연락처는 010-1234-5678 입니다", "PHONE"),
        ("카드 1234-5678-9012-3456 를 씁니다", "CARD"),
        ("토큰 sk-abcdefghijklmnop 을 저장했습니다", "API_KEY"),
    ],
)
def test_sensitive_values_are_reported_by_field_not_quoted(value: str, label: str):
    """지적 문장은 프롬프트와 로그에 그대로 실린다. **값을 인용하면 안 된다.**

    패턴이 겹쳐 한 값이 두 번 걸릴 수 있다(전화번호는 ACCOUNT 형태이기도 하다).
    같은 값을 두 줄로 지적하는 것은 모델에게 해가 없으므로 개수를 고정하지 않는다.
    """

    memory = UserMemory(personality=value)

    violations = find_violations(memory)

    assert violations
    assert any(label in item for item in violations)
    assert all("personality" in item for item in violations)
    assert all(value not in item for item in violations)


def test_sensitive_values_inside_custom_attributes_are_reported():
    memory = UserMemory(custom_attributes={"연락": "010-1234-5678"})

    violations = find_violations(memory)

    assert violations and "customAttributes.연락" in violations[0]


def test_serialized_size_ignores_metadata_and_empty_fields():
    """상한이 지키려는 것은 프롬프트 토큰이고, 프롬프트에 실리는 것은 projection 이다."""

    empty = UserMemory(updated_at="2026-08-06T09:00:00+09:00")

    assert serialized_chars(empty) == len("{}")
