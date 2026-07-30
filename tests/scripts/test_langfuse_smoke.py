"""합성 Langfuse 연결 smoke가 실제 Timeline trace와 구분되는지 검증한다."""

import pytest

from scripts import langfuse_smoke


def _payload() -> dict:
    return {
        "name": langfuse_smoke.TRACE_NAME,
        "tags": ["smoke", "synthetic"],
        "observations": [
            {
                "id": "root",
                "name": langfuse_smoke.ROOT_NAME,
                "type": "CHAIN",
                "input": {"contentLengthBytes": 22},
            },
            {
                "id": "generation",
                "parentObservationId": "root",
                "name": langfuse_smoke.GENERATION_NAME,
                "type": "GENERATION",
                "model": "synthetic-smoke-model",
                "usageDetails": {"input": 11, "output": 7, "total": 18},
            },
            {
                "id": "tool",
                "parentObservationId": "root",
                "name": langfuse_smoke.TOOL_NAME,
                "type": "TOOL",
            },
        ],
    }


def test_smoke_contract_is_synthetic_and_does_not_impersonate_timeline() -> None:
    names = langfuse_smoke._validate_trace_payload(_payload())

    assert langfuse_smoke.TRACE_NAME != "generate-timeline"
    assert "generate-timeline" not in names
    assert "main-agent" not in names
    assert names == {
        "verify-langfuse-connectivity",
        "verify-generation-usage",
        "verify-tool-observation",
    }


def test_smoke_contract_rejects_unmasked_content() -> None:
    payload = _payload()
    payload["observations"][0]["input"] = "smoke-user@example.com"

    with pytest.raises(RuntimeError, match="노출"):
        langfuse_smoke._validate_trace_payload(payload)
