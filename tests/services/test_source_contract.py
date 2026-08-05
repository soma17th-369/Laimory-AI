"""입력 조회 응답의 묶음 단위 계약 검증."""

import pytest
from pydantic import ValidationError

from app.core.error_codes import ErrorCode
from app.schemas import ItemType, TimelineInputResponse
from app.services.source_contract import (
    SourceBatchError,
    ensure_source_contract,
    source_raw_ids,
)
from tests.fixtures.requests import fixture_raw_id, make_snapshot, source_item

_TASK_ID = "task-contract"


def _snapshot(**overrides):
    defaults = dict(
        task_id=_TASK_ID,
        source_items=[
            source_item(1, ItemType.STAY, {"latitude": 37.5, "longitude": 127.0})
        ],
    )
    defaults.update(overrides)
    return make_snapshot(**defaults)


def test_valid_snapshot_passes():
    ensure_source_contract(_TASK_ID, _snapshot())


def test_task_id_mismatch_is_rejected():
    with pytest.raises(SourceBatchError, match="다른 task"):
        ensure_source_contract(_TASK_ID, _snapshot(task_id="other-task"))


def test_empty_source_items_is_rejected():
    with pytest.raises(SourceBatchError, match="수집 원본이 없습니다"):
        ensure_source_contract(_TASK_ID, _snapshot(source_items=[]))


def test_duplicate_raw_ids_are_rejected():
    duplicated = fixture_raw_id("dup")
    snapshot = _snapshot(
        source_items=[
            source_item(1, ItemType.STAY, {}, raw_id="dup"),
            source_item(2, ItemType.PHOTO, {}, raw_id="dup"),
        ]
    )

    with pytest.raises(SourceBatchError, match="중복 rawId") as caught:
        ensure_source_contract(_TASK_ID, snapshot)

    assert duplicated in caught.value.detail


def test_violation_carries_the_contract_error_code():
    """콜백·관측·로그가 같은 정수를 쓰도록 예외가 코드를 들고 있어야 한다."""

    with pytest.raises(SourceBatchError) as caught:
        ensure_source_contract(_TASK_ID, _snapshot(source_items=[]))

    assert caught.value.code is ErrorCode.SOURCE_CONTRACT_VIOLATION


def test_violation_message_is_not_the_external_message():
    """진단 문장은 로그용이고, 밖으로 나가는 문장은 카탈로그 메시지다."""

    with pytest.raises(SourceBatchError) as caught:
        ensure_source_contract(_TASK_ID, _snapshot(task_id="other-task"))

    assert _TASK_ID in caught.value.detail
    assert _TASK_ID not in caught.value.message


def test_source_raw_ids_collects_every_item():
    snapshot = _snapshot(
        source_items=[
            source_item(1, ItemType.STAY, {}, raw_id="a"),
            source_item(2, ItemType.PHOTO, {}, raw_id="b"),
        ]
    )

    assert source_raw_ids(snapshot) == {fixture_raw_id("a"), fixture_raw_id("b")}


def test_input_response_maps_window_to_snapshot():
    """API 계약(`window.startAt`)과 내부 계약(`timelineWindow.startTime`)의 다리."""

    response = TimelineInputResponse.model_validate(
        {
            "taskId": _TASK_ID,
            "recordDate": "2026-07-22",
            "recordTimeZone": "Asia/Seoul",
            "window": {
                "startAt": "2026-07-22T00:00:00+09:00",
                "endAt": "2026-07-23T00:00:00+09:00",
            },
            "sourceItems": [
                {
                    "rawId": fixture_raw_id("input-1"),
                    "itemType": "PHOTO",
                    "startAt": "2026-07-22T12:00:00+09:00",
                    "endAt": None,
                    "payload": {"filename": "p.jpg"},
                }
            ],
        }
    )

    snapshot = response.to_snapshot()

    assert snapshot.task_id == _TASK_ID
    assert snapshot.record_date == "2026-07-22"
    assert snapshot.timeline_window.start_time == "2026-07-22T00:00:00+09:00"
    assert snapshot.user_memory is None
    assert snapshot.source_items[0].item_type is ItemType.PHOTO


# --- userMemory (#65) ---------------------------------------------------


def _input_response(**overrides) -> TimelineInputResponse:
    body = {
        "taskId": _TASK_ID,
        "recordDate": "2026-07-22",
        "recordTimeZone": "Asia/Seoul",
        "sourceItems": [],
    }
    body.update(overrides)
    return TimelineInputResponse.model_validate(body)


def test_user_memory_is_parsed_and_carried_into_snapshot():
    response = _input_response(
        userMemory={"schemaVersion": "1.0", "basicProfile": "30대 개발자"}
    )

    memory = response.parse_user_memory()
    snapshot = response.to_snapshot(user_memory=memory)

    assert snapshot.user_memory is not None
    assert snapshot.user_memory.basic_profile == "30대 개발자"


@pytest.mark.parametrize("body", [{}, {"userMemory": None}])
def test_missing_or_null_user_memory_keeps_previous_behavior(body):
    """계약상 선택 필드다. 없다고 실패로 보지 않는다(하위 호환)."""

    response = _input_response(**body)

    assert response.parse_user_memory() is None
    assert response.to_snapshot().user_memory is None


@pytest.mark.parametrize(
    "user_memory",
    [
        pytest.param({"favoriteColor": "파랑"}, id="unknown-field"),
        pytest.param({"schemaVersion": "2.0"}, id="unsupported-version"),
        pytest.param({"basicProfile": "가" * 201}, id="over-length"),
        pytest.param(
            {"customAttributes": {f"k{i}": "v" for i in range(6)}},
            id="too-many-custom-attributes",
        ),
    ],
)
def test_contract_violation_is_raised_for_the_caller_to_absorb(user_memory):
    """스키마는 애매하게 받아 주지 않는다. 흡수 여부는 호출 경계가 정한다."""

    response = _input_response(userMemory=user_memory)

    with pytest.raises(ValidationError):
        response.parse_user_memory()
