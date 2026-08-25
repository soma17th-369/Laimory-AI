"""애플리케이션 공통 설정 검증."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "app_env": "test",
        "log_level": "INFO",
        "llm_provider": "bedrock",
        "app_server_api_url": "https://api.example.com/s/api/v1",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_app_server_api_url_normalizes_trailing_slash() -> None:
    configured = _settings(
        app_server_api_url=" https://api.example.com/s/api/v1/ "
    )

    assert configured.app_server_api_url == "https://api.example.com/s/api/v1"


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/s/api/v1",
        # 배포 환경에 따라 접두사가 /s/v1 인 곳도 있다.
        "http://10.0.4.78:8080/s/v1",
    ],
)
def test_app_server_api_url_accepts_both_version_prefixes(value: str) -> None:
    assert _settings(app_server_api_url=value).app_server_api_url == value


def test_blank_app_server_api_url_is_rejected() -> None:
    """App Server API 는 유일한 데이터 경로다(이슈 #40). 비어 있으면 뜨지 않는다."""

    with pytest.raises(ValidationError):
        _settings(app_server_api_url=" ")


def test_app_server_api_url_is_required(monkeypatch) -> None:
    # 실행 환경에 값이 있으면 "없을 때" 를 볼 수 없다. 환경변수까지 걷어낸다.
    monkeypatch.delenv("APP_SERVER_API_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(
            app_env="test",
            log_level="INFO",
            llm_provider="bedrock",
            _env_file=None,
        )


def test_app_server_retry_defaults() -> None:
    configured = _settings()

    assert configured.app_server_timeout_sec == 3.0
    assert configured.app_server_max_attempts == 3
    assert configured.app_server_retry_backoff_sec == 0.5


def test_app_server_timeout_sec_is_read_from_environment(monkeypatch) -> None:
    """기본값을 3초로 줄여도(이슈 #86) 명시 설정이 계속 이긴다."""

    monkeypatch.setenv("APP_SERVER_TIMEOUT_SEC", "7")

    assert _settings().app_server_timeout_sec == 7.0


def test_prompt_version_defaults_to_v1(monkeypatch) -> None:
    monkeypatch.delenv("PROMPT_VERSION", raising=False)

    assert _settings().prompt_version == "v1"


def test_prompt_version_is_normalized() -> None:
    assert _settings(prompt_version=" V1 ").prompt_version == "v1"


def test_prompt_version_is_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("PROMPT_VERSION", " V1 ")

    assert _settings().prompt_version == "v1"


def test_provider_models_and_server_bindings_are_read_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-env-test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-env-test")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("SERVER_PORT", "8080")

    configured = _settings()

    assert configured.openai_model == "gpt-env-test"
    assert configured.gemini_model == "gemini-env-test"
    assert configured.server_host == "0.0.0.0"
    assert configured.server_port == 8080


def test_supported_prompt_versions_are_accepted() -> None:
    assert _settings(prompt_version="v2").prompt_version == "v2"
    assert _settings(prompt_version=" V2 ").prompt_version == "v2"


def test_unknown_prompt_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(prompt_version="v3")


def test_app_server_max_attempts_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        _settings(app_server_max_attempts=0)


def test_elasticsearch_settings_are_gone() -> None:
    """앱은 Elasticsearch 를 직접 호출하지 않는다(이슈 #47).

    접속 정보를 설정으로 되살리면 직접 전송 경로도 함께 돌아온다. 운영 로그는
    stdout JSON 을 Filebeat 가 실어 나르고, 그 자격증명은 EC2 에만 둔다.
    """

    configured = _settings()

    for removed in (
        "obs_enabled",
        "obs_content_capture",
        "obs_max_payload_bytes",
        "obs_max_events_per_task",
        "obs_local_dir",
        "es_url",
        "es_api_key",
        "es_event_index",
        "es_timeout_sec",
        "es_max_retries",
    ):
        assert not hasattr(configured, removed), removed


def test_langfuse_defaults_to_japan_region_with_content_disabled() -> None:
    configured = _settings()

    assert configured.langfuse_base_url == "https://jp.cloud.langfuse.com"
    assert configured.langfuse_content_capture is None
    assert configured.langfuse_capture_policy == "NONE"
    assert configured.langfuse_max_payload_bytes == 64 * 1024


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        ("local", "SANITIZED"),
        ("dev", "SANITIZED"),
        ("DEV", "SANITIZED"),
        ("prod", "NONE"),
        ("timeline-audit", "NONE"),
    ],
)
def test_langfuse_capture_policy_follows_app_env(app_env: str, expected: str) -> None:
    """설정을 비워 두면 실행 환경이 정한다(이슈 #48).

    dev 기본값이 NONE 이라 trace 에 해시만 남고 디버깅이 불가능했다.
    """

    assert _settings(app_env=app_env).langfuse_capture_policy == expected


@pytest.mark.parametrize("app_env", ["local", "dev", "prod"])
def test_explicit_langfuse_content_capture_always_wins(app_env: str) -> None:
    configured = _settings(app_env=app_env, langfuse_content_capture="NONE")

    assert configured.langfuse_capture_policy == "NONE"


def test_langfuse_base_url_normalizes_trailing_slash() -> None:
    configured = _settings(
        langfuse_base_url=" https://jp.cloud.langfuse.com/ "
    )

    assert configured.langfuse_base_url == "https://jp.cloud.langfuse.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"langfuse_sample_rate": -0.1},
        {"langfuse_sample_rate": 1.1},
        {"langfuse_max_payload_bytes": 0},
        {"langfuse_base_url": "jp.cloud.langfuse.com"},
        {"langfuse_base_url": "https://jp.cloud.langfuse.com?tenant=1"},
    ],
)
def test_langfuse_rejects_invalid_settings(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


@pytest.mark.parametrize(
    "value",
    [
        "api.example.com/s/api/v1",
        "ftp://api.example.com/s/api/v1",
        "/s/api/v1",
        "https://api.example.com/api/v1",
        "https://api.example.com/s/api/v1?tenant=1",
        "https://api.example.com/s/api/v1#callback",
    ],
)
def test_app_server_api_url_rejects_invalid_base_url(value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(app_server_api_url=value)
