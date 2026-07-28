"""애플리케이션 공통 설정 검증."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        app_env="test",
        log_level="INFO",
        llm_provider="bedrock",
        _env_file=None,
        **overrides,
    )


def test_app_server_api_url_normalizes_trailing_slash() -> None:
    configured = _settings(
        app_server_api_url=" https://api.example.com/s/api/v1/ "
    )

    assert configured.app_server_api_url == "https://api.example.com/s/api/v1"


def test_blank_app_server_api_url_disables_callback() -> None:
    assert _settings(app_server_api_url=" ").app_server_api_url is None


def test_observation_content_capture_defaults_to_sanitized() -> None:
    configured = _settings()

    assert configured.obs_content_capture == "SANITIZED"
    assert configured.obs_max_payload_bytes == 256 * 1024


def test_observation_content_capture_rejects_unknown_policy() -> None:
    with pytest.raises(ValidationError):
        _settings(obs_content_capture="RAW")


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
