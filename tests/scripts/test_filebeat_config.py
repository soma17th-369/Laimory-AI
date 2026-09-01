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
        "url",
        "query",
        "body",
    ):
        assert field in dropped, f"방어선에서 빠진 필드: {field}"


def test_the_collected_error_detail_fields_survive_the_second_line(processors) -> None:
    """운영 이벤트가 싣는 오류 상세는 수집기에서 지우지 않는다(#109 범위 확장).

    이름이 점 표기(`error.message`)와 camelCase(`errorMessage`)로 갈린 이유가 이것이다.
    앞은 표식 없는 줄에서 새어 든 값이라 버리고, 뒤는 emitter 의 allowlist 를 통과해
    마스킹·길이 상한까지 거친 값이라 남긴다. 방어선을 넓히다 이 둘을 함께 지우면
    prod 에서 원인을 볼 수단이 조용히 사라진다 — `docker logs` 라는 대안이 없다.
    """

    dropped = {
        field
        for processor in processors
        if "drop_fields" in processor
        for field in processor["drop_fields"]["fields"]
    }

    assert not dropped & {"errorMessage", "errorStackTrace"}


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
# --- 이름 충돌 (이슈 #109) ---------------------------------------------------
#
# 표식과 순서가 맞아도 **필드 이름 하나**로 이벤트가 통째로 사라질 수 있다. Filebeat 는
# 자기 수집기 정보를 `agent.*` 객체로 붙이는데, 앱이 같은 이름으로 문자열을 실으면
# `decode_json_fields`(`target: ""`, `overwrite_keys: true`)가 그 객체를 덮는다. 그러면
# 한 문서 안에서 `agent` 가 문자열이 되어 data stream 의 object mapping 과 충돌하고
# Elasticsearch 가 그 문서만 거절한다 — dev EC2 에서 `app.degraded` 만 적재되지 않았다.
#
# 앱은 점 없는 최상위 이름을 쓰고 수집기는 객체를 만든다는 것이 이 경로의 사실이므로,
# 여기서는 그 둘이 겹치지 않는지만 본다. 점이 든 이름(`event.dataset`, `log.level`,
# `error.type`)은 검사 대상이 아니다 — 펴지면 양쪽 다 객체라 충돌하지 않는다.

#: 수집기가 **객체**로 채우는 최상위 이름 중 설정에서 유도할 수 없는 것.
#: `add_host_metadata` 가 `host.*` 를, container 입력이 `container.*` 와
#: `log.file.path`/`log.offset` 을 붙인다.
_PIPELINE_OBJECT_FIELDS = frozenset({"host", "container", "log"})


def _object_namespaces(processors: list[dict]) -> set[str]:
    """이 파이프라인에서 객체가 되는 최상위 이름들.

    `drop_fields` 의 점 있는 항목이 곧 근거다 — `agent.id` 를 지운다는 것은 이 경로에서
    `agent` 가 객체라는 뜻이다. 설정이 바뀌면 이 목록도 따라 바뀐다.
    """

    derived = {
        field.split(".")[0]
        for processor in processors
        if "drop_fields" in processor
        for field in processor["drop_fields"]["fields"]
        if "." in field
    }
    return derived | set(_PIPELINE_OBJECT_FIELDS)


def _application_scalar_fields() -> set[str]:
    """앱이 최상위에 **점 없이** 내보내는 이름 전체(고정 필드 + 모든 이벤트 allowlist)."""

    from app.core.logging import _RESERVED_FIELDS
    from app.core.operational_logging import _ALLOWED_FIELDS

    names = set(_RESERVED_FIELDS)
    for allowed in _ALLOWED_FIELDS.values():
        names |= set(allowed)
    return {name for name in names if "." not in name}


def test_application_field_names_never_collide_with_collector_objects(
    processors,
) -> None:
    """겹치면 그 이벤트는 로그에 찍히고도 Elasticsearch 에 남지 않는다."""

    collisions = _application_scalar_fields() & _object_namespaces(processors)

    assert not collisions, (
        "수집기가 객체로 쓰는 이름을 앱이 최상위 문자열로 내보내고 있습니다: "
        f"{sorted(collisions)}"
    )


def test_the_agent_name_field_keeps_the_beats_agent_object_intact() -> None:
    """#109 의 회귀. Agent 이름은 `agentName` 으로 나가고 `agent` 는 수집기 몫이다."""

    from app.core.operational_logging import _ALLOWED_FIELDS, OperationalEvent

    degraded = _ALLOWED_FIELDS[OperationalEvent.APP_DEGRADED]
    assert "agentName" in degraded
    assert "agent" not in degraded
