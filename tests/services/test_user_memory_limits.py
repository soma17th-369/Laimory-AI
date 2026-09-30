"""User Memory 갱신의 크기 정책 (#64).

여기서 지키는 두 가지가 계약이다.

- 입력 batch는 schema가 최대 5건으로 거절한다. 그 안의 event·본문은 자르며,
  자를 때 메모 있는 event를 끝까지 남긴다. 사용자가 고른 하루 감정은 날짜 항목에
  싣는다(#121).
- 출력은 **자르지 않고 지적한다.** 압축은 의미 판단이라 코드가 문장을 건드리지 않는다.
"""

import pytest

from app.schemas.user_memory import NARRATIVE_MAX_LENGTH, UserMemory, UserMemoryPatch
from app.schemas.user_memory_update import DailyTimeline
from app.services.event_count_guard import MAX_EVENT_COUNT as TIMELINE_MAX_EVENT_COUNT
from app.services.user_memory_limits import (
    EMOTION_MAX_CHARS,
    MAX_DAILY_TIMELINE_COUNT,
    MAX_EVENT_COUNT,
    MAX_EVENTS_PER_TIMELINE,
    MEMO_MAX_CHARS,
    TEXT_MAX_CHARS,
    USER_MEMORY_MAX_CHARS,
    USER_MEMORY_TARGET_CHARS,
    build_daily_timeline_digest,
    drop_removals,
    find_violations,
    serialized_chars,
    shrink_budget,
)
from tests.fixtures.user_memory import change, daily_timeline, daily_timeline_event


def _entries(payload: list[dict]) -> list[DailyTimeline]:
    return [DailyTimeline.model_validate(item) for item in payload]


# --- 입력 잘라내기 -----------------------------------------------------


def test_digest_keeps_only_the_most_recent_timelines():
    payload = [
        daily_timeline(record_date=f"2026-07-{day:02d}")
        for day in range(1, MAX_DAILY_TIMELINE_COUNT + 4)
    ]

    digest = build_daily_timeline_digest(_entries(payload))

    assert digest.stats["dailyTimelineCount"] == MAX_DAILY_TIMELINE_COUNT
    assert digest.stats["droppedDailyTimelineCount"] == 3
    # 남은 것은 최근 날짜이고, 프롬프트에는 오름차순으로 실린다.
    dates = [entry["date"] for entry in digest.daily_timelines]
    assert dates == sorted(dates)
    assert dates[-1] == f"2026-07-{MAX_DAILY_TIMELINE_COUNT + 3:02d}"


def test_digest_keeps_memo_events_when_over_budget():
    """메모는 event 안에서 사용자가 직접 쓴 유일한 글이다. 마지막까지 지킨다."""

    events = [
        daily_timeline_event(
            title=f"이벤트 {index}",
            start_at=f"2026-08-04T{index % 24:02d}:00:00+09:00",
            end_at=None,
        )
        for index in range(MAX_EVENTS_PER_TIMELINE + 10)
    ]
    # 가장 오래된 자리에 메모를 둔다. 시간 순으로만 자르면 이것부터 사라진다.
    events[0]["memo"] = "오늘은 오랜만에 마음이 놓였어요."
    events[0]["startAt"] = "2026-08-04T00:00:00+09:00"

    digest = build_daily_timeline_digest(_entries([daily_timeline(events=events)]))

    memos = [
        event.get("memo")
        for entry in digest.daily_timelines
        for event in entry["events"]
        if event.get("memo")
    ]
    assert digest.stats["eventCount"] == MAX_EVENTS_PER_TIMELINE
    assert digest.stats["droppedEventCount"] == 10
    assert memos == ["오늘은 오랜만에 마음이 놓였어요."]


def test_digest_reports_memo_count_even_for_dropped_events():
    """센 것은 접수한 전부다. 자른 뒤 숫자만 보면 "메모 없는 날" 로 오해한다."""

    events = [
        daily_timeline_event(start_at=f"2026-08-04T{index % 24:02d}:30:00+09:00", end_at=None)
        for index in range(MAX_EVENTS_PER_TIMELINE + 5)
    ]
    for event in events:
        event["memo"] = "메모"

    digest = build_daily_timeline_digest(_entries([daily_timeline(events=events)]))

    assert digest.stats["memoCount"] == MAX_EVENTS_PER_TIMELINE + 5
    assert digest.has_memo


def test_digest_reports_no_memo_day():
    digest = build_daily_timeline_digest(_entries([daily_timeline()]))

    assert digest.stats["memoCount"] == 0
    assert not digest.has_memo


def test_digest_drops_the_minute_from_event_times():
    """분 단위 시각은 갱신 판단에 쓸모가 없고 프로필 문장에 샐 위험만 만든다."""

    digest = build_daily_timeline_digest(
        _entries([daily_timeline(events=[daily_timeline_event(start_at="2026-08-04T12:43:00+09:00")])])
    )

    event = digest.daily_timelines[0]["events"][0]
    assert event["hour"] == 12
    assert "startAt" not in event
    assert "43" not in str(event)


def test_digest_omits_question_and_empty_values():
    """`question` 도 AI 가 쓴 문장이라 갱신 근거로 주지 않는다."""

    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(
                    events=[
                        daily_timeline_event(
                            subtitle=None,
                            question="어떤 이야기가 기억에 남았나요?",
                            memo="  ",
                        )
                    ]
                )
            ]
        )
    )

    event = digest.daily_timelines[0]["events"][0]
    assert "question" not in event
    assert "subtitle" not in event
    assert "memo" not in event


def test_digest_clips_long_text_instead_of_rejecting():
    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(
                    events=[
                        daily_timeline_event(title="가" * 400, memo="나" * (MEMO_MAX_CHARS + 200))
                    ]
                )
            ]
        )
    )

    event = digest.daily_timelines[0]["events"][0]
    assert len(event["title"]) == TEXT_MAX_CHARS
    assert len(event["memo"]) == MEMO_MAX_CHARS


def test_digest_skips_a_day_whose_events_were_all_dropped():
    """event 가 하나도 안 남은 날은 싣지 않는다. 모델이 "아무 일도 없던 날" 로 읽는다."""

    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(record_date="2026-08-03", events=[]),
                daily_timeline(),
            ]
        )
    )

    assert [entry["date"] for entry in digest.daily_timelines] == ["2026-08-04"]


def test_digest_of_nothing_is_empty_not_an_error():
    digest = build_daily_timeline_digest([])

    assert digest.daily_timelines == []
    assert digest.stats["eventCount"] == 0
    assert digest.stats["emotionCount"] == 0


# --- 하루 감정 (#121) ---------------------------------------------------


def test_digest_carries_the_emotion_the_user_picked():
    """감정은 하루에 하나다. event 가 아니라 날짜 항목에 싣는다."""

    digest = build_daily_timeline_digest(
        _entries([daily_timeline(emotion_type="VERY_HAPPY")])
    )

    entry = digest.daily_timelines[0]
    assert entry["emotion"] == "VERY_HAPPY"
    assert all("emotion" not in event for event in entry["events"])
    assert digest.stats["emotionCount"] == 1


@pytest.mark.parametrize("emotion_type", [None, "", "   "])
def test_digest_omits_the_emotion_key_when_none_was_picked(emotion_type):
    """감정을 받기 전에 저장된 기록은 값이 없다.

    빈 값을 남기면 모델이 "감정이 없던 날" 을 근거로 삼을 수 있다.
    """

    digest = build_daily_timeline_digest(
        _entries([daily_timeline(emotion_type=emotion_type)])
    )

    assert "emotion" not in digest.daily_timelines[0]
    assert digest.stats["emotionCount"] == 0


def test_digest_passes_an_unknown_emotion_through():
    """값을 enum 으로 좁히지 않는다. App Server 가 값을 더해도 갱신이 죽지 않는다."""

    digest = build_daily_timeline_digest(
        _entries([daily_timeline(emotion_type="EXCITED")])
    )

    assert digest.daily_timelines[0]["emotion"] == "EXCITED"


def test_digest_clips_an_oversized_emotion():
    digest = build_daily_timeline_digest(
        _entries([daily_timeline(emotion_type="가" * (EMOTION_MAX_CHARS + 50))])
    )

    assert len(digest.daily_timelines[0]["emotion"]) == EMOTION_MAX_CHARS


def test_emotion_count_counts_days_not_events():
    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(record_date="2026-08-03", emotion_type="UNHAPPY"),
                daily_timeline(record_date="2026-08-04", emotion_type=None),
                daily_timeline(record_date="2026-08-05", emotion_type="HAPPY"),
            ]
        )
    )

    assert digest.stats["emotionCount"] == 2


def test_emotion_of_a_day_without_events_is_not_carried():
    """event 가 하나도 없는 날은 통째로 싣지 않는다. 감정도 함께 빠지고 세지 않는다."""

    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(record_date="2026-08-03", emotion_type="HAPPY", events=[]),
                daily_timeline(record_date="2026-08-04", emotion_type=None),
            ]
        )
    )

    assert [entry["date"] for entry in digest.daily_timelines] == ["2026-08-04"]
    assert digest.stats["emotionCount"] == 0


# --- 끝 시각 (#121) -----------------------------------------------------


def test_digest_carries_the_end_hour_without_minutes():
    """시작만 있으면 그 일을 얼마 동안 했는지 알 수 없다."""

    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(
                    events=[
                        daily_timeline_event(
                            start_at="2026-08-04T09:12:00+09:00",
                            end_at="2026-08-04T18:47:00+09:00",
                        )
                    ]
                )
            ]
        )
    )

    event = digest.daily_timelines[0]["events"][0]
    assert event["hour"] == 9
    assert event["endHour"] == 18
    assert "endAt" not in event
    assert "47" not in str(event)


def test_digest_omits_the_end_hour_of_a_single_point_event():
    digest = build_daily_timeline_digest(
        _entries([daily_timeline(events=[daily_timeline_event(end_at=None)])])
    )

    assert "endHour" not in digest.daily_timelines[0]["events"][0]


def test_end_hour_of_an_event_crossing_midnight_is_smaller_than_its_start():
    digest = build_daily_timeline_digest(
        _entries(
            [
                daily_timeline(
                    events=[
                        daily_timeline_event(
                            start_at="2026-08-04T23:10:00+09:00",
                            end_at="2026-08-05T01:20:00+09:00",
                        )
                    ]
                )
            ]
        )
    )

    event = digest.daily_timelines[0]["events"][0]
    assert (event["hour"], event["endHour"]) == (23, 1)


# --- 출력 검사 ---------------------------------------------------------


def test_clean_memory_has_no_violations():
    memory = UserMemory(basic_profile="30대 개발자입니다.")

    assert find_violations(memory) == []


def _oversized_memory() -> UserMemory:
    """필드는 저마다 상한 안인데 합치면 전체 상한을 넘는 문서."""

    return UserMemory(
        **{
            field: "가" * NARRATIVE_MAX_LENGTH
            for field in (
                "basic_profile",
                "life_context",
                "relationships",
                "personality",
                "values",
            )
        }
    )


def test_total_cap_is_2000_chars():
    """값이 바뀌면 프롬프트·문서가 말하는 숫자도 함께 바뀌어야 한다(#121)."""

    assert USER_MEMORY_MAX_CHARS == 2_000


def test_oversized_memory_is_reported_without_being_cut():
    memory = _oversized_memory()

    violations = find_violations(memory)

    assert serialized_chars(memory) > USER_MEMORY_MAX_CHARS
    assert len(violations) == 1
    assert str(USER_MEMORY_MAX_CHARS) in violations[0]
    # 지적했을 뿐 문장은 그대로다.
    assert len(memory.basic_profile) == NARRATIVE_MAX_LENGTH


def test_size_violation_leaves_the_order_of_cuts_to_the_prompt():
    """무엇부터 줄일지는 갱신 정책이고 정책은 프롬프트 세트가 갖는다(#121).

    순서를 코드에 박아 두면 재요청 때 시스템 프롬프트와 다른 말을 한다. 예전 문장은
    "오래된 단기 관심사 제거" 를 앞에 뒀는데, v3 는 정보를 지우는 것이 마지막이다.
    """

    violation = find_violations(_oversized_memory())[0]

    assert "「크기」" in violation
    for policy_word in ("제거", "병합", "중복", "관심사", "오래된"):
        assert policy_word not in violation


def test_size_violation_says_how_much_in_sentences_per_item():
    """얼마나 줄일지는 코드가 말한다. 모델이 스스로 세지 못하는 값이다(#121).

    단위는 문장 수다. 같은 문서로 실측했을 때 전체 글자 수 지시는 1%, 항목별 글자 수는
    5%, 항목별 문장 수는 14% 가 줄었고 앞의 둘로는 상한 아래로 내려오지 못했다.
    """

    memory = UserMemory(
        basic_profile="첫 문장입니다. 둘째 문장입니다. 셋째 문장입니다. " + "가" * 450,
        life_context="나" * NARRATIVE_MAX_LENGTH,
        relationships="다" * NARRATIVE_MAX_LENGTH,
        personality="라" * NARRATIVE_MAX_LENGTH,
        values="마" * NARRATIVE_MAX_LENGTH,
    )

    violation = find_violations(memory)[0]

    assert str(USER_MEMORY_TARGET_CHARS) in violation
    assert "`basicProfile`: 지금 4문장 → 1문장 이내" in violation
    # 줄일 수 없는 항목(한 문장짜리)은 적지 않는다. 적힌 항목만 줄인다는 뜻이다.
    assert "`lifeContext`" not in violation
    assert "남긴 문장도 짧게 다시 쓰세요" in violation
    # 값은 어디에도 없다.
    assert "첫 문장" not in violation
    assert "가가" not in violation


# --- 줄일 몫 (#121) -----------------------------------------------------


def test_no_budget_when_the_document_fits_the_target():
    assert shrink_budget(UserMemory(basic_profile="30대 개발자입니다.")) == []


def test_target_is_below_the_cap():
    """모델이 겨냥하는 값이 상한과 같으면 넘치는 몫을 받아 낼 자리가 없다."""

    assert USER_MEMORY_TARGET_CHARS < USER_MEMORY_MAX_CHARS
    assert USER_MEMORY_TARGET_CHARS == 1_600


def test_budget_shares_the_cut_in_proportion_to_sentence_counts():
    """덜어 낼 문장은 항목의 문장 수에 비례해 나눈다."""

    ten = " ".join(f"문장 {index}번입니다." for index in range(10))
    five = " ".join(f"문장 {index}번입니다." for index in range(5))
    memory = UserMemory(basic_profile=ten, life_context=five)

    budget = shrink_budget(memory, target_chars=serialized_chars(memory) * 3 // 5)

    cuts = {
        line.split("`")[1]: int(line.split("지금 ")[1].split("문장")[0])
        - int(line.split("→ ")[1].split("문장")[0])
        for line in budget
    }
    assert cuts["basicProfile"] == 2 * cuts["lifeContext"]


def test_budget_cuts_only_as_much_as_the_excess():
    """목표를 조금 넘었으면 조금만 줄인다.

    예전에는 항목마다 비율을 내림해, 일곱 자를 넘었을 뿐인데 두 문장 이상인 항목이 전부
    한 문장씩 줄었다. 실제 모델이 그대로 따라 485자를 지웠다(사는 곳까지).
    """

    sentences = " ".join(f"문장 {index}번입니다." for index in range(6))
    memory = UserMemory(
        basic_profile=sentences,
        life_context=sentences,
        relationships=sentences,
        custom_attributes={"운동": sentences, "여행": sentences},
    )

    budget = shrink_budget(memory, target_chars=serialized_chars(memory) - 7)

    assert len(budget) == 1
    assert budget[0].endswith("지금 6문장 → 5문장 이내")


def test_budget_covers_the_excess():
    """덜어 낸 문장의 글자 수가 넘은 글자 수에 닿아야 한다. 모자라면 다음 날도 넘는다."""

    sentences = " ".join(f"문장 {index}번입니다." for index in range(8))
    memory = UserMemory(
        basic_profile=sentences, routines=sentences, custom_attributes={"운동": sentences}
    )
    size = serialized_chars(memory)
    need = size // 3

    budget = shrink_budget(memory, target_chars=size - need)

    per_sentence = len(sentences) // 8
    removed = sum(
        int(line.split("지금 ")[1].split("문장")[0]) - int(line.split("→ ")[1].split("문장")[0])
        for line in budget
    )
    assert removed * per_sentence >= need
    assert (removed - 1) * per_sentence < need, "필요한 것보다 한 문장 넘게 줄이지 않습니다."


def test_budget_breaks_ties_from_the_last_item():
    """몫이 같으면 뒤 항목부터 줄인다. 앞에 놓인 고정 필드보다 속성이 먼저다."""

    sentences = " ".join(f"문장 {index}번입니다." for index in range(4))
    memory = UserMemory(
        basic_profile=sentences,
        relationships=sentences,
        custom_attributes={"운동": sentences},
    )

    budget = shrink_budget(memory, target_chars=serialized_chars(memory) - 1)

    assert budget == ["`customAttributes.운동`: 지금 4문장 → 3문장 이내"]


def test_budget_never_asks_for_zero_sentences():
    memory = UserMemory(basic_profile="하나입니다.", life_context="둘입니다. 셋입니다.")

    budget = shrink_budget(memory, target_chars=10)

    assert "`lifeContext`: 지금 2문장 → 1문장 이내" in budget
    # 한 문장짜리 항목은 문장 수로 줄일 수 없어 적지 않는다.
    assert not any(line.startswith("`basicProfile`") for line in budget)
    assert budget[-1].startswith("문장 수를 맞춰도 목표를 넘습니다")


def test_budget_names_custom_attributes_by_key_without_their_values():
    memory = UserMemory(
        custom_attributes={"반려동물": "고양이를 키웁니다. 병원에 다녀왔습니다."}
    )

    budget = shrink_budget(memory, target_chars=10)

    assert budget[0] == "`customAttributes.반려동물`: 지금 2문장 → 1문장 이내"
    assert all("고양이" not in line for line in budget)


def test_budget_asks_for_fewer_custom_attributes_when_sentences_cannot_shrink():
    """한 문장짜리 항목은 문장 수로 줄일 수 없다.

    개수 제한이 없는 자리라 문서가 커지는 쪽은 대개 여기다. 문장 수를 맞춰도 목표를
    넘으면 항목 수의 몫을 함께 준다.
    """

    memory = UserMemory(
        custom_attributes={f"속성{index}": "한 문장짜리 값입니다." for index in range(40)}
    )
    size = serialized_chars(memory)

    budget = shrink_budget(memory, target_chars=size // 2)

    assert "`customAttributes` 항목 수: 지금 40개 → 20개 이내" in budget
    assert budget[-1].startswith("문장 수를 맞춰도 목표를 넘습니다")


def test_budget_does_not_touch_the_attribute_count_when_sentences_are_enough():
    sentences = " ".join(f"문장 {index}번입니다." for index in range(10))
    memory = UserMemory(
        basic_profile=sentences,
        custom_attributes={"반려동물": "고양이를 키웁니다."},
    )

    budget = shrink_budget(memory, target_chars=serialized_chars(memory) * 3 // 4)

    assert not any("항목 수" in line for line in budget)


def test_many_small_custom_attributes_are_bounded_by_the_total_cap():
    """개수 제한이 없어졌으므로 끝을 막는 것은 전체 상한 하나다(#121)."""

    memory = UserMemory(
        custom_attributes={f"속성{index}": "가" * 40 for index in range(60)}
    )

    violations = find_violations(memory)

    assert serialized_chars(memory) > USER_MEMORY_MAX_CHARS
    assert len(violations) == 1


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


# --- 날짜별 몫 ---------------------------------------------------------


def test_each_day_gets_its_own_event_quota():
    """event 가 몰린 하루가 다른 날의 자리를 먹지 않는다.

    전체 상한 하나로 자르면 정렬 기준이 (메모 있음, 최근순)이라 최근의 바쁜 하루가
    앞자리를 다 차지하고, 밀려난 날은 payload 에서 빠져 모델에게는 애초에 없던 날이
    된다. 여러 날에 걸쳐 같은 일이 있었는지 볼 근거가 통째로 사라진다.
    """

    busy = daily_timeline(
        record_date="2026-08-05",
        events=[
            daily_timeline_event(
                start_at=f"2026-08-05T{index % 24:02d}:00:00+09:00", end_at=None
            )
            for index in range(MAX_EVENTS_PER_TIMELINE + 30)
        ],
    )
    quiet = daily_timeline(
        record_date="2026-08-04",
        events=[daily_timeline_event(start_at="2026-08-04T09:00:00+09:00", end_at=None)],
    )

    digest = build_daily_timeline_digest(_entries([busy, quiet]))

    by_date = {entry["date"]: entry["events"] for entry in digest.daily_timelines}
    assert set(by_date) == {"2026-08-04", "2026-08-05"}
    assert len(by_date["2026-08-05"]) == MAX_EVENTS_PER_TIMELINE
    assert len(by_date["2026-08-04"]) == 1
    assert digest.stats["droppedEventCount"] == 30


def test_total_cap_is_derived_from_the_daily_quota():
    """전체 상한을 따로 정하지 않는다. 두 값이 갈리면 어느 쪽이 이기는지 알기 어렵다."""

    assert MAX_EVENT_COUNT == MAX_DAILY_TIMELINE_COUNT * MAX_EVENTS_PER_TIMELINE


def test_daily_quota_is_the_timeline_event_cap():
    """하루 몫은 Timeline v3 가 하루를 구성하는 event 의 최대 개수와 같다(#119 에서 10개).

    두 값이 갈리면 한쪽만 바뀐 것이다. Timeline 의 상한을 바꿀 때 이 몫을 어떻게 할지도
    함께 정해야 한다.
    """

    assert MAX_EVENTS_PER_TIMELINE == TIMELINE_MAX_EVENT_COUNT == 10


def test_a_full_request_never_exceeds_the_total_cap():
    payload = [
        daily_timeline(
            record_date=f"2026-08-{day:02d}",
            events=[
                daily_timeline_event(
                    start_at=f"2026-08-{day:02d}T{index % 24:02d}:00:00+09:00",
                    end_at=None,
                )
                for index in range(MAX_EVENTS_PER_TIMELINE + 5)
            ],
        )
        for day in range(1, MAX_DAILY_TIMELINE_COUNT + 3)
    ]

    digest = build_daily_timeline_digest(_entries(payload))

    assert digest.stats["eventCount"] == MAX_EVENT_COUNT
    assert digest.stats["dailyTimelineCount"] == MAX_DAILY_TIMELINE_COUNT


# --- 기존 내용은 지우지 않는다 (#121) -----------------------------------
#
# 달라졌으면 고쳐 쓰는 것이고, 이번 기록에 나오지 않았으면 그대로 두는 것이다. 코드가
# 잡는 것은 "지우기만 하는" 변경이다 — 고쳐 쓰면서 내용을 빠뜨리는 것은 의미를 봐야
# 알 수 있어 프롬프트가 맡는다.


def _changes(*changes: dict) -> UserMemoryPatch:
    return UserMemoryPatch.model_validate({"changes": list(changes)})


def _small_profile() -> UserMemory:
    return UserMemory(
        basic_profile="판교 회사에서 일합니다. 망원동에 삽니다.",
        routines="평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다. 저녁에 산책합니다.",
        custom_attributes={"악기": "기타를 배웁니다."},
    )


def _items(patch: UserMemoryPatch) -> list[str]:
    return [item.item for item in patch.changes]


@pytest.mark.parametrize("item", ["routines", "customAttributes.악기"])
def test_removal_of_an_existing_item_is_dropped(item: str):
    patch, dropped = drop_removals(_changes(change(item, "삭제")), _small_profile())

    assert dropped == 1
    assert patch.changes == []


@pytest.mark.parametrize(
    "text",
    [
        "평일에는 회사에서 일합니다.",
        "평일에는 회사에서 일합니다. 저녁에 산책합니다.",
        "저녁에 산책합니다. 평일에는 회사에서 일합니다.",
    ],
)
def test_update_that_only_drops_sentences_is_dropped(text: str):
    """기존 문장 가운데 일부만 남긴 것. 순서를 바꿔도 지운 것은 지운 것이다."""

    patch, dropped = drop_removals(_changes(change("routines", "수정", text)), _small_profile())

    assert dropped == 1
    assert patch.changes == []


@pytest.mark.parametrize(
    "text",
    [
        # 달라져서 바뀐 것 — 옛 내용이 새 내용으로 바뀌었다.
        "재택으로 일합니다. 주말에 클라이밍을 합니다. 저녁에 산책합니다.",
        # 합친 것 — 문장 수는 줄었지만 새 문장이 있다.
        "평일에는 회사에서 일하고 주말에 클라이밍을 합니다.",
        # 더한 것.
        "평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다. 저녁에 산책합니다. 아침에 러닝을 합니다.",
        # 그대로 다시 낸 것 — 지우지 않는다(바뀌는 것도 없다).
        "평일에는 회사에서 일합니다. 주말에 클라이밍을 합니다. 저녁에 산책합니다.",
    ],
)
def test_update_that_changes_or_adds_is_kept(text: str):
    patch, dropped = drop_removals(_changes(change("routines", "수정", text)), _small_profile())

    assert dropped == 0
    assert _items(patch) == ["routines"]


def test_cancelled_plan_is_rewritten_not_removed():
    """계획을 취소했으면 지우는 것이 아니라 달라진 내용을 적는다. 그 변경은 막지 않는다."""

    memory = UserMemory(custom_attributes={"여행 계획": "10월에 강릉에 갈 계획입니다."})
    rewritten = _changes(
        change("customAttributes.여행 계획", "수정", "10월에 강릉에 가려던 계획을 취소했습니다.")
    )

    patch, dropped = drop_removals(rewritten, memory)

    assert dropped == 0
    assert patch.apply_to(memory).custom_attributes == {
        "여행 계획": "10월에 강릉에 가려던 계획을 취소했습니다."
    }


def test_removal_of_something_that_is_not_there_is_not_counted():
    """비어 있는 항목과 없는 속성은 지울 것이 없다. 적용해도 아무 일도 일어나지 않는다."""

    patch, dropped = drop_removals(
        _changes(change("lifeContext", "삭제"), change("customAttributes.없는 키", "삭제")),
        _small_profile(),
    )

    assert dropped == 0
    assert patch.apply_to(_small_profile()) == _small_profile()


def test_add_is_never_a_removal():
    patch, dropped = drop_removals(
        _changes(change("routines", "추가", "평일에는 회사에서 일합니다.")), _small_profile()
    )

    assert dropped == 0
    assert _items(patch) == ["routines"]


def test_other_changes_in_the_same_list_survive():
    patch, dropped = drop_removals(
        _changes(
            change("customAttributes.악기", "삭제"),
            change("basicProfile", "수정", "강남 회사에서 일합니다. 망원동에 삽니다."),
            change("lifeContext", "추가", "이직한 지 얼마 안 된 시기입니다."),
        ),
        _small_profile(),
    )

    assert dropped == 1
    assert _items(patch) == ["basicProfile", "lifeContext"]


@pytest.mark.parametrize("memory", [None, UserMemory()])
def test_nothing_is_dropped_from_an_empty_profile(memory):
    given = _changes(change("routines", "삭제"), change("basicProfile", "추가", "직장인입니다."))

    patch, dropped = drop_removals(given, memory)

    assert dropped == 0
    assert patch is given


def _over_target_profile() -> UserMemory:
    sentences = " ".join(f"문장 {index}번입니다." for index in range(40))
    return UserMemory(
        basic_profile=sentences[:NARRATIVE_MAX_LENGTH],
        life_context=sentences[:NARRATIVE_MAX_LENGTH],
        relationships=sentences[:NARRATIVE_MAX_LENGTH],
        personality=sentences[:NARRATIVE_MAX_LENGTH],
        custom_attributes={"악기": "기타를 배웁니다."},
    )


def test_item_with_a_shrink_budget_may_be_shortened():
    """목표를 넘은 문서는 줄여야 한다. 막으면 문서가 상한에 닿은 뒤로 갱신이 매번
    1304 로 끝난다."""

    memory = _over_target_profile()
    assert serialized_chars(memory) > USER_MEMORY_TARGET_CHARS
    assert any(line.startswith("`personality`") for line in shrink_budget(memory))
    given = _changes(change("personality", "수정", "문장 0번입니다."))

    patch, dropped = drop_removals(given, memory)

    assert dropped == 0
    assert patch is given


def test_item_without_a_shrink_budget_is_kept_even_over_the_target():
    """목표를 넘은 날에도 지울 수 있는 것은 줄일 몫을 받은 항목뿐이다."""

    memory = _over_target_profile()
    assert not any("악기" in line for line in shrink_budget(memory))

    patch, dropped = drop_removals(
        _changes(
            change("customAttributes.악기", "삭제"),
            change("personality", "수정", "문장 0번입니다."),
        ),
        memory,
    )

    assert dropped == 1
    assert _items(patch) == ["personality"]


def test_whole_field_is_never_removed():
    """몫은 언제나 한 문장 이상이다. 고정 필드를 통째로 비우는 변경은 적용하지 않는다."""

    patch, dropped = drop_removals(
        _changes(change("personality", "삭제")), _over_target_profile()
    )

    assert dropped == 1
    assert patch.changes == []


def test_attribute_may_be_removed_only_when_the_budget_asks_for_fewer_attributes():
    """한 문장짜리 속성이 많아 문장 수로는 줄일 수 없을 때만 속성을 통째로 지울 수 있다."""

    memory = UserMemory(
        custom_attributes={f"속성{index}": "가" * 40 + "입니다." for index in range(50)}
    )
    assert serialized_chars(memory) > USER_MEMORY_TARGET_CHARS
    assert any("항목 수" in line for line in shrink_budget(memory))
    given = _changes(change("customAttributes.속성3", "삭제"))

    patch, dropped = drop_removals(given, memory)

    assert dropped == 0
    assert patch is given


def test_removals_are_dropped_when_no_shrink_budget_is_given():
    """지우기를 허용하는 조건이 `[크기]` 절이 몫을 주는 조건과 어긋나면, 프롬프트는
    줄이라고 하는데 코드가 막거나 그 반대가 된다."""

    memory = _small_profile()

    assert shrink_budget(memory) == []
    assert drop_removals(_changes(change("routines", "삭제")), memory)[1] == 1
