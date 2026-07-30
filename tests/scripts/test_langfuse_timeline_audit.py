"""실제 Timeline Langfuse audit의 API 계약 검증."""

import pytest

from scripts import langfuse_timeline_audit


def _observation(
    observation_id: str,
    name: str,
    observation_type: str,
    parent_id: str | None,
) -> dict:
    payload = {
        "id": observation_id,
        "name": name,
        "type": observation_type,
        "input": {"marker": "합성 프로젝트 회의"},
        "output": {"durationMs": 1.0},
    }
    if parent_id is not None:
        payload["parentObservationId"] = parent_id
    return payload


def _payload() -> dict:
    root = _observation("root", "generate-timeline", "SPAN", None)
    root["output"] = {
        "durationMs": 120.0,
        "tokenUsage": {
            "generationCount": 7,
            "inputTokens": 70,
            "outputTokens": 35,
            "totalTokens": 105,
            "byType": {"input": 70, "output": 35},
        },
    }
    main = _observation(
        "main",
        "main-agent",
        "AGENT",
        "root",
    )
    observations = [root, main]
    for index, name in enumerate(
        sorted(
            langfuse_timeline_audit._REQUIRED_AGENT_NAMES
            - {"main-agent"}
        )
    ):
        observations.append(_observation(f"agent-{index}", name, "AGENT", "main"))

    boundary_types = {
        name: "CHAIN" if name == "merge-event-results" else "SPAN"
        for name in langfuse_timeline_audit._REQUIRED_BOUNDARY_NAMES
    }
    for index, (name, observation_type) in enumerate(boundary_types.items()):
        parent = "main" if name == "merge-event-results" else "root"
        observations.append(
            _observation(f"boundary-{index}", name, observation_type, parent)
        )

    for index, name in enumerate(
        sorted(langfuse_timeline_audit._REQUIRED_GENERATION_NAMES)
    ):
        generation = _observation(
            f"generation-{index}",
            name,
            "GENERATION",
            "agent-0",
        )
        generation["model"] = "synthetic-model"
        generation["usageDetails"] = {"input": 10, "output": 5}
        observations.append(generation)
    return {
        "name": "generate-timeline",
        "sessionId": "task-searchable",
        "environment": "timeline-audit",
        "tags": ["timeline"],
        "observations": observations,
    }


def test_audit_accepts_full_timeline_contract() -> None:
    result = langfuse_timeline_audit._audit_trace(
        _payload(),
        expected_task_id="task-searchable",
    )

    assert result["generationCount"] == 7
    assert result["tokenUsage"]["totalTokens"] == 105
    assert result["contentVisible"] is True


def test_audit_rejects_callback_token_exposure() -> None:
    payload = _payload()
    payload["observations"][0]["input"] = (
        langfuse_timeline_audit._CALLBACK_SENTINEL
    )

    with pytest.raises(RuntimeError, match="callback token"):
        langfuse_timeline_audit._audit_trace(payload)


def test_audit_rejects_missing_task_session() -> None:
    with pytest.raises(RuntimeError, match="sessionId"):
        langfuse_timeline_audit._audit_trace(
            _payload(),
            expected_task_id="different-task",
        )
