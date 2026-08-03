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


def test_unrealistic_transport_used_without_uncertainty_is_warned():
    request = make_request(
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
    result = _result(
        candidates=[
            candidate(
                "MOVEMENT",
                [("MOVEMENT", fixture_raw_id("movement-m1"))],
                uncertainty=(),
            )
        ]
    )

    verify_location_result(result, request)

    assert "평균 속도로" in _messages(result)


def test_empty_location_input_is_a_no_op():
    result = _result()

    verify_location_result(result, make_request())

    assert result.warnings == []
