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
