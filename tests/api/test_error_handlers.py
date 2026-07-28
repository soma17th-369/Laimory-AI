"""전역 예외 처리기가 모든 실패를 하나의 오류 계약으로 내보내는지 검증한다.

계약: ``{"errorCode": <int>, "error": <비어 있지 않은 문자열>}``
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.error_handlers import register_error_handlers
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import AppError
from app.schemas.error import ErrorResponse
from app.server import app as real_app
from app.services.timeline_validator import (
    TimelineValidationError,
    TimelineViolation,
    TimelineViolationCode,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(real_app, raise_server_exceptions=False)


@pytest.fixture
def failing_client() -> TestClient:
    """실패만 내는 임시 앱. 실제 라우터를 건드리지 않고 처리기만 본다."""

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/unexpected")
    def unexpected():
        raise RuntimeError("내부 경로 /srv/secret 실패, token=abc123")

    @app.get("/domain")
    def domain():
        raise TimelineValidationError(
            [
                TimelineViolation(
                    TimelineViolationCode.SOURCE_RAW_ID_NOT_IN_TASK,
                    "source rawId=leak-me-1234 가 task 입력에 없습니다",
                )
            ]
        )

    @app.get("/app-error")
    def app_error():
        raise AppError("내부 진단 문구", code=ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND)

    @app.get("/http-error")
    def http_error():
        raise HTTPException(status_code=409, detail="Conflict")

    return TestClient(app, raise_server_exceptions=False)


def _assert_contract(body: dict) -> None:
    """어떤 실패든 이 형태여야 한다."""

    assert set(body) == {"errorCode", "error"}
    assert isinstance(body["errorCode"], int)
    assert isinstance(body["error"], str) and body["error"].strip()


def test_request_validation_error_returns_1001(client: TestClient) -> None:
    response = client.post("/v1/timeline", json={"taskId": "t-1"})

    assert response.status_code == 422
    _assert_contract(response.json())
    assert response.json() == {
        "errorCode": int(ErrorCode.REQUEST_VALIDATION_FAILED),
        "error": message_for(ErrorCode.REQUEST_VALIDATION_FAILED),
    }


def test_validation_error_does_not_echo_the_submitted_input(client: TestClient) -> None:
    """FastAPI 기본 422 는 입력값을 그대로 돌려준다. 그걸 막았는지 본다."""

    response = client.post(
        "/v1/timeline",
        json={"taskId": "leak-me-1234", "callbackToken": "super-secret-token"},
    )

    assert response.status_code == 422
    assert "leak-me-1234" not in response.text
    assert "super-secret-token" not in response.text
    assert "callbackToken" not in response.text


def test_malformed_json_body_returns_1001(client: TestClient) -> None:
    response = client.post(
        "/v1/timeline",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == int(ErrorCode.REQUEST_VALIDATION_FAILED)


def test_unknown_path_returns_1003(client: TestClient) -> None:
    response = client.get("/v1/does-not-exist")

    assert response.status_code == 404
    _assert_contract(response.json())
    assert response.json()["errorCode"] == int(ErrorCode.NOT_FOUND)


def test_wrong_method_returns_1004(client: TestClient) -> None:
    response = client.delete("/v1/timeline")

    assert response.status_code == 405
    assert response.json()["errorCode"] == int(ErrorCode.METHOD_NOT_ALLOWED)


def test_invocations_uses_the_same_contract_as_v1_timeline(client: TestClient) -> None:
    """`/invocations` 는 `/v1/timeline` 어댑터다. 오류도 같아야 한다."""

    v1 = client.post("/v1/timeline", json={})
    invocations = client.post("/invocations", json={})

    assert v1.status_code == invocations.status_code == 422
    assert v1.json() == invocations.json()


def test_unexpected_exception_returns_1901_without_leaking(
    failing_client: TestClient,
) -> None:
    response = failing_client.get("/unexpected")

    assert response.status_code == 500
    _assert_contract(response.json())
    assert response.json()["errorCode"] == int(ErrorCode.INTERNAL_ERROR)
    assert "/srv/secret" not in response.text
    assert "abc123" not in response.text
    assert "RuntimeError" not in response.text


def test_domain_exception_keeps_its_own_code(failing_client: TestClient) -> None:
    """코드를 가진 예외가 미분류 방어선으로 흘러가 1901 로 뭉개지면 안 된다."""

    response = failing_client.get("/domain")

    assert response.json()["errorCode"] == int(
        ErrorCode.TIMELINE_STORAGE_VALIDATION_FAILED
    )
    assert "leak-me-1234" not in response.text


def test_app_error_message_comes_from_the_catalog(failing_client: TestClient) -> None:
    response = failing_client.get("/app-error")

    assert response.json() == {
        "errorCode": int(ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND),
        "error": message_for(ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND),
    }
    assert "내부 진단 문구" not in response.text


def test_error_response_rejects_unknown_reserved_or_custom_message() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(error_code=9999, error="임의 오류")
    with pytest.raises(ValidationError):
        ErrorResponse(
            error_code=int(ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED),
            error=message_for(ErrorCode.LEGACY_TIMELINE_GENERATION_FAILED),
        )
    with pytest.raises(ValidationError):
        ErrorResponse(
            error_code=int(ErrorCode.INTERNAL_ERROR),
            error="원본 예외 메시지",
        )


def test_http_exception_keeps_its_status_but_uses_our_body(
    failing_client: TestClient,
) -> None:
    """401/403/409 를 400 으로 뭉개면 클라이언트가 원인을 구분하지 못한다."""

    response = failing_client.get("/http-error")

    assert response.status_code == 409
    _assert_contract(response.json())
    assert response.json()["errorCode"] == int(ErrorCode.BAD_REQUEST)
    assert "Conflict" not in response.text


def test_openapi_documents_the_common_error_contract(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    for path in ("/v1/timeline", "/invocations"):
        responses = spec["paths"][path]["post"]["responses"]
        for status_code in ("422", "500"):
            schema = responses[status_code]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ErrorResponse")

    error_schema = spec["components"]["schemas"]["ErrorResponse"]
    assert error_schema["properties"]["errorCode"]["type"] == "integer"
    assert error_schema["properties"]["error"]["type"] == "string"
    assert sorted(error_schema["required"]) == ["error", "errorCode"]
