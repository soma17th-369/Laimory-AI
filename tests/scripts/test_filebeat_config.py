"""Filebeat 수집 경계의 정적 계약을 검증한다 (이슈 #53).

이 템플릿이 틀리면 두 가지 중 하나가 일어난다. 조건이 느슨하면 사용자 데이터가
Elasticsearch 로 흘러가고, 순서나 필드 경로가 틀리면 **모든 운영 로그가 사라진다**.
둘 다 배포한 뒤에야 알아채므로 여기서 형태를 고정한다.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

CONFIG = (
    Path(__file__).parents[2] / "docs" / "observability" / "filebeat.example.yml"
)

#: 앱의 수집 표식(app/core/operational_logging.py 의 EVENT_DATASET 과 같은 값).
EVENT_DATASET = "laimory.api"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def processors(config) -> list[dict]:
    return config["processors"]


def _index_of(processors: list[dict], name: str) -> int:
    for index, processor in enumerate(processors):
        if name in processor:
            return index
    raise AssertionError(f"processor 없음: {name}")


def test_marker_matches_the_application_constant() -> None:
    """앱과 수집기가 다른 값을 쓰면 조용히 아무것도 적재되지 않는다."""

    from app.core.operational_logging import EVENT_DATASET as app_dataset

    assert app_dataset == EVENT_DATASET


def test_json_decoding_expands_dotted_keys(processors) -> None:
    """`event.dataset` 은 점이 든 키다. 펴지 않으면 아래 조건이 매칭되지 않는다."""

    decode = processors[_index_of(processors, "decode_json_fields")][
        "decode_json_fields"
    ]
    assert decode["expand_keys"] is True
    assert decode["fields"] == ["message"]
    assert decode["target"] == ""


def test_events_without_the_marker_are_dropped(processors) -> None:
    """fail-closed. 표식이 없으면 버린다 — 일반 로그가 늘어도 수집은 안 늘어난다."""

    drop = processors[_index_of(processors, "drop_event")]["drop_event"]
    assert drop["when"]["not"]["equals"]["event.dataset"] == EVENT_DATASET


def test_the_marker_filter_runs_right_after_decoding(processors) -> None:
    """decode 앞에 두면 표식을 읽을 수 없어 모든 이벤트가 사라진다."""

    assert _index_of(processors, "decode_json_fields") < _index_of(
        processors, "drop_event"
    )


def test_sensitive_and_free_text_fields_are_dropped_as_a_second_line(
    processors,
) -> None:
    dropped = {
        field
        for processor in processors
        if "drop_fields" in processor
        for field in processor["drop_fields"]["fields"]
    }

    for field in (
        "taskToken",
        "authorization",
        "apiKey",
        "error.message",
        "error.stack_trace",
        "errorMessage",
        "url",
        "query",
        "body",
    ):
        assert field in dropped, f"방어선에서 빠진 필드: {field}"


def test_event_marker_fields_are_never_dropped(processors) -> None:
    """표식을 지우면 이벤트 종류를 구분할 수단이 사라진다."""

    dropped = {
        field
        for processor in processors
        if "drop_fields" in processor
        for field in processor["drop_fields"]["fields"]
    }

    assert not dropped & {"event.dataset", "event.action", "event.outcome", "event"}


def test_collection_stays_limited_to_the_application_container(config) -> None:
    """컨테이너 한정 수집(이슈 #47)은 그대로 유지한다."""

    template = config["filebeat.autodiscover"]["providers"][0]["templates"][0]
    assert template["condition"]["equals"]["docker.container.name"] == "laimory-ai"


def test_output_target_and_timestamp_handling_are_unchanged(config, processors) -> None:
    assert config["output.elasticsearch"]["index"] == "logs-laimory.ai-${LAIMORY_ENV}"
    timestamp = processors[_index_of(processors, "timestamp")]["timestamp"]
    assert timestamp["field"] == "timestamp"
