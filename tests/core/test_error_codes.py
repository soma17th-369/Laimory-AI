"""오류 코드 카탈로그의 계약을 지킨다.

여기서 깨지면 "같은 실패가 어디서나 같은 코드로 나간다"는 전제가 무너진다.
"""

import re
from pathlib import Path

import pytest

from app.core.error_codes import (
    DEFAULT_HTTP_STATUS,
    ERROR_MESSAGES,
    RESERVED_CODES,
    ErrorCode,
    code_for_http_status,
    http_status_for,
    message_for,
)


def test_every_code_has_a_unique_integer() -> None:
    values = [int(code) for code in ErrorCode]

    assert len(values) == len(set(values))


def test_every_code_has_a_non_empty_message() -> None:
    """계약이 `error` 를 "비어 있지 않은 문자열"로 정의한다."""

    for code in ErrorCode:
        assert message_for(code).strip(), f"{code.name} 에 메시지가 없습니다"


def test_messages_cover_exactly_the_catalog() -> None:
    assert set(ERROR_MESSAGES) == set(ErrorCode)


@pytest.mark.parametrize(
    ("code", "band"),
    [
        (ErrorCode.REQUEST_VALIDATION_FAILED, 1000),
        (ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND, 1100),
        (ErrorCode.PIPELINE_TIMEOUT, 1200),
        (ErrorCode.TIMELINE_STORAGE_VALIDATION_FAILED, 1300),
        (ErrorCode.CALLBACK_SEND_FAILED, 1400),
        (ErrorCode.INTERNAL_ERROR, 1900),
    ],
)
def test_codes_sit_in_their_declared_band(code: ErrorCode, band: int) -> None:
    assert band <= int(code) < band + 100


def test_legacy_1008_is_reserved_and_never_emitted() -> None:
    """구 계약의 `"ERROR_1008"` 번호는 재사용하지 않는다.

    같은 실패에 코드가 둘이면 이슈가 없애려던 혼란이 그대로 남는다. 분류되지 않은
    최종 실패는 `INTERNAL_ERROR` 하나로 모은다.
    """

    assert ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED in RESERVED_CODES
    assert int(ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED) == 1008


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (422, ErrorCode.REQUEST_VALIDATION_FAILED),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (400, ErrorCode.BAD_REQUEST),
        (401, ErrorCode.BAD_REQUEST),
        (409, ErrorCode.BAD_REQUEST),
        (500, ErrorCode.INTERNAL_ERROR),
        (503, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_http_status_maps_to_catalog_code(
    status_code: int, expected: ErrorCode
) -> None:
    assert code_for_http_status(status_code) is expected


def test_background_only_codes_fall_back_to_500() -> None:
    """HTTP 로 나갈 일이 없던 코드가 응답 경로에 닿아도 4xx 로 오해되지 않는다."""

    assert http_status_for(ErrorCode.PIPELINE_TIMEOUT) == DEFAULT_HTTP_STATUS
    assert http_status_for(ErrorCode.DATABASE_ERROR) == 500


def test_docs_table_matches_the_catalog() -> None:
    """`docs/error-codes.md` 의 코드 표가 카탈로그와 어긋나지 않게 막는다.

    클라이언트는 이 문서를 보고 분기를 짠다. 코드를 추가하면서 표를 빼먹으면 문서만
    보는 쪽은 그 코드가 없는 줄 안다.
    """

    doc_path = Path(__file__).resolve().parents[2] / "docs" / "error-codes.md"
    rows = re.findall(
        r"^\| (\d{4}) \| `(\w+)` \|",
        doc_path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    documented = {int(code): name for code, name in rows}
    # 예약 번호는 이름 없이 *(예약)* 으로만 적혀 있어 표 파싱 대상이 아니다.
    catalog = {int(code): code.name for code in ErrorCode if code not in RESERVED_CODES}

    assert documented == catalog


def test_messages_do_not_leak_internal_markers() -> None:
    """외부 메시지에 내부 식별자·경로·예외 클래스명이 섞이지 않았는지 본다."""

    forbidden = ("taskId", "rawId", "Traceback", "Error(", "/app/", "sqlalchemy")
    for code in ErrorCode:
        message = message_for(code)
        for marker in forbidden:
            assert marker not in message, f"{code.name} 메시지에 {marker} 가 있습니다"
