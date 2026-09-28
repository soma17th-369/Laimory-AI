"""Location 결과 검증 (#56 §4.4 검증 코드).

Location Agent 는 v2 에서 상위 여정 복원과 데이터 공백 표시를 맡았다. 확률적 판단이라
조용히 안 될 수 있어서, 입력과 결과를 대조해 그 실패를 찾는다. 고치지는 않는다 —
여정을 어떻게 묶을지는 의미 판단이고, 코드가 대신 묶으면 근거 없는 event 가 생긴다.
"""

from app.schemas import AgentEventResult
from app.services.location_guard import verify_location_result
from tests.fixtures.fake_llm import candidate, fragment
from tests.fixtures.requests import fixture_raw_id, make_request, movement_item, stay_item


def _result(candidates=None, fragments=None) -> AgentEventResult:
    return AgentEventResult.model_validate(
        {"candidates": candidates or [], "fragments": fragments or []}
    )


def _long_movements():
    return [
        movement_item("m1", start="2026-06-20T09:00:00", end="2026-06-20T11:00:00", distance=80_000),
        movement_item("m2", start="2026-06-20T11:10:00", end="2026-06-20T11:50:00", distance=25_000),
    ]


def _messages(result: AgentEventResult) -> str:
    return " ".join(warning.message for warning in result.warnings)


def test_long_distance_without_journey_candidate_is_warned():
    request = make_request(movements=_long_movements())
    result = _result(
        candidates=[
            candidate("MOVEMENT", [("MOVEMENT", fixture_raw_id("movement-m1"))]),
            candidate("MOVEMENT", [("MOVEMENT", fixture_raw_id("movement-m2"))]),
        ]
    )

    verify_location_result(result, request)

    assert "하나의 여정으로 묶은" in _messages(result)


def test_journey_candidate_covering_both_movements_is_clean():
    request = make_request(movements=_long_movements())
    result = _result(
        candidates=[
            candidate(
                "MOVEMENT",
                [
                    ("MOVEMENT", fixture_raw_id("movement-m1")),
                    ("MOVEMENT", fixture_raw_id("movement-m2")),
                ],
            )
        ]
    )

    verify_location_result(result, request)

    assert "여정" not in _messages(result)


def test_coverage_gap_without_uncertainty_is_flagged():
    request = make_request(
        stays=[stay_item("s1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00")]
    )
    result = _result(
        candidates=[
            candidate("REST", [("STAY", fixture_raw_id("stay-s1"))], uncertainty=())
        ]
    )

    verify_location_result(result, request)

    assert "공백" in _messages(result)


def test_coverage_gap_mentioned_in_uncertainty_is_clean():
    request = make_request(
        stays=[stay_item("s1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00")]
    )
    item = candidate("REST", [("STAY", fixture_raw_id("stay-s1"))])
    item["uncertainty"] = ["10시 이후 위치 기록이 없어 이후 행적을 확정할 수 없다."]
    result = _result(candidates=[item])

    verify_location_result(result, request)

    assert "공백" not in _messages(result)


def test_dropped_raw_id_is_flagged():
    request = make_request(
        stays=[
            stay_item("s1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00"),
            stay_item("s2", start="2026-06-20T11:00:00", end="2026-06-20T23:50:00"),
        ]
    )
    result = _result(
        candidates=[candidate("REST", [("STAY", fixture_raw_id("stay-s1"))])]
    )

    verify_location_result(result, request)

    assert "후보에도 단서에도 남지 않았습니다" in _messages(result)


def test_raw_id_kept_as_fragment_is_not_flagged():
    """단서로만 남아도 유실이 아니다. fragment 가 그러라고 있는 자리다."""

    request = make_request(
        stays=[
            stay_item("s1", start="2026-06-20T09:00:00", end="2026-06-20T10:00:00"),
            stay_item("s2", start="2026-06-20T11:00:00", end="2026-06-20T23:50:00"),
        ]
    )
    result = _result(
        candidates=[candidate("REST", [("STAY", fixture_raw_id("stay-s1"))])],
        fragments=[fragment("STAY", fixture_raw_id("stay-s2"), "짧은 체류 단서")],
    )

    verify_location_result(result, request)

    assert "남지 않았습니다" not in _messages(result)


def _walk_labelled_car_ride():
    """도보 라벨인데 한 시간에 60km 를 간 이동. 라벨과 속도가 서로 다른 쪽을 가리킨다."""

    return make_request(
        movements=[
            movement_item(
                "m1",
                start="2026-06-20T09:00:00",
                end="2026-06-20T10:00:00",
                distance=60_000,
                transports=["WALKING"],
            )
        ]
    )


def test_conflicting_movement_mode_used_without_uncertainty_is_warned():
    result = _result(
        candidates=[
            candidate(
                "MOVEMENT",
                [("MOVEMENT", fixture_raw_id("movement-m1"))],
                uncertainty=(),
            )
        ]
    )

    verify_location_result(result, _walk_labelled_car_ride())

    assert "도보·이동수단 이용" in _messages(result)


def test_conflicting_movement_mode_with_uncertainty_is_clean():
    result = _result(
        candidates=[
            candidate(
                "MOVEMENT",
                [("MOVEMENT", fixture_raw_id("movement-m1"))],
                uncertainty=("이동 방식 라벨과 속도가 맞지 않음",),
            )
        ]
    )

    verify_location_result(result, _walk_labelled_car_ride())

    assert "도보·이동수단 이용" not in _messages(result)


def test_empty_location_input_is_a_no_op():
    result = _result()

    verify_location_result(result, make_request())

    assert result.warnings == []


# --- 이동 사이 장시간 체류 (#119) -------------------------------------------------


def _commute(stay_end: str = "18:00"):
    """집을 나서 → 머물고 → 돌아온 하루."""

    return make_request(
        movements=[
            movement_item("out", start="2026-06-20T08:20:00", end="2026-06-20T09:00:00"),
            movement_item("back", start=f"2026-06-20T{stay_end}:00", end="2026-06-20T19:00:00"),
        ],
        stays=[
            stay_item("office", start="2026-06-20T09:00:00", end=f"2026-06-20T{stay_end}:00")
        ],
    )


_COMMUTE_REFS = [
    ("MOVEMENT", fixture_raw_id("movement-out")),
    ("STAY", fixture_raw_id("stay-office")),
    ("MOVEMENT", fixture_raw_id("movement-back")),
]


def test_long_stay_absorbed_into_a_journey_candidate_is_flagged():
    """20분 기준을 어겨 몇 시간짜리 체류를 이동 하나로 합친 candidate 를 잡는다."""

    result = _result(candidates=[candidate("MOVEMENT", _COMMUTE_REFS)])

    verify_location_result(result, _commute())

    assert "20분을 넘게 머문 체류 1건이 앞뒤 이동과 하나의 후보로" in _messages(result)
    # 사실만 말한다. Repair 는 candidate 를 고칠 수 없어 지시는 끝까지 남는다.
    assert "나눠야" not in _messages(result)


def test_short_stay_inside_a_journey_candidate_is_clean():
    """짧은 센서 분절을 낀 정상적인 연속 이동은 그대로 둔다."""

    result = _result(candidates=[candidate("MOVEMENT", _COMMUTE_REFS)])

    verify_location_result(result, _commute(stay_end="09:15"))

    assert "20분을 넘게" not in _messages(result)


def test_separate_candidates_for_journey_and_stay_are_clean():
    result = _result(
        candidates=[
            candidate("MOVEMENT", _COMMUTE_REFS[:1]),
            candidate("WORK", _COMMUTE_REFS[1:2]),
            candidate("MOVEMENT", _COMMUTE_REFS[2:]),
        ]
    )

    verify_location_result(result, _commute())

    assert "20분을 넘게" not in _messages(result)


def test_legacy_sets_are_not_told_about_the_long_stay():
    """이 warning 은 Timeline 을 거쳐 Repair 가 읽는다. 나눌 도구가 없는 세트에는 보이지 않는다."""

    result = _result(candidates=[candidate("MOVEMENT", _COMMUTE_REFS)])

    verify_location_result(result, _commute(), check_long_stay=False)

    assert "20분을 넘게" not in _messages(result)


def test_guard_does_not_split_the_candidate():
    result = _result(candidates=[candidate("MOVEMENT", _COMMUTE_REFS)])

    verify_location_result(result, _commute())

    assert len(result.candidates) == 1
    assert len(result.candidates[0].source_refs) == 3
