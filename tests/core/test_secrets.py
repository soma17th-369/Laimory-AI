"""시크릿 번들 단위 테스트(이슈 #30).

번들이 환경변수·`.env` 를 이기는 정본이라는 것, 비밀 아닌 설정도 담을 수 있다는 것,
1회 캐시, 조회 실패 흡수, 값 미노출을 확인한다. 실제 AWS 는 호출하지 않는다.
"""

import json
import logging

import pytest

from app.core import secret_bundle
from app.core import secrets as secrets_module
from app.core.config import Settings
from app.core.error_codes import ErrorCode
from app.core.llm import OpenAIProvider

_BUNDLE = "laimory/test"


def _diagnostic(caplog) -> logging.LogRecord:
    """`report_error` 가 남긴 **진단 줄**을 고른다(이슈 #101).

    같은 호출이 표식 달린 `app.degraded` 운영 이벤트도 함께 내므로, `records[-1]` 은
    이제 그쪽을 집는다. 여기서 보려는 것은 자유 필드가 남는 진단 줄이다.
    """

    records = [record for record in caplog.records if record.name != "app.operational"]
    assert records, "진단 줄이 없습니다."
    return records[-1]


@pytest.fixture(autouse=True)
def _clear_bundle_cache():
    secret_bundle.reset_cache()
    yield
    secret_bundle.reset_cache()


class _FakeSecretsManager:
    """`get_secret_value` 만 흉내 내는 테스트 더블."""

    def __init__(self, payload: str = "", error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls = 0
        self.secret_ids: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
        self.calls += 1
        self.secret_ids.append(SecretId)
        if self._error is not None:
            raise self._error
        return {"SecretString": self._payload}


def _use_bundle(monkeypatch, mapping=None, *, name=_BUNDLE, error=None):
    """번들 이름을 환경변수로 주고 AWS 호출을 테스트 더블로 바꾼다."""

    client = _FakeSecretsManager(json.dumps(mapping or {}), error)
    monkeypatch.setenv(secret_bundle.BUNDLE_NAME_ENV, name)
    monkeypatch.setattr(secret_bundle, "_secrets_manager_client", lambda: client)
    return client


def test_empty_bundle_name_never_calls_aws(monkeypatch):
    """번들 이름이 없으면 AWS 를 부르지 않는다. 로컬·테스트가 이 성질에 기댄다."""

    def _forbidden():
        raise AssertionError("번들 이름이 없으면 AWS 를 호출하면 안 된다.")

    monkeypatch.delenv(secret_bundle.BUNDLE_NAME_ENV, raising=False)
    monkeypatch.setattr(secret_bundle, "_secrets_manager_client", _forbidden)
    monkeypatch.setenv("BEDROCK_MODEL", "from-env")

    assert Settings().bedrock_model == "from-env"


def test_bundle_wins_over_environment(monkeypatch):
    """정본은 하나다 — env 파일에 옛 값이 남아 있어도 번들이 이긴다."""

    _use_bundle(monkeypatch, {"BEDROCK_MODEL": "from-bundle"})
    monkeypatch.setenv("BEDROCK_MODEL", "from-env")

    assert Settings().bedrock_model == "from-bundle"


def test_bundle_carries_non_secret_settings(monkeypatch):
    """비밀뿐 아니라 환경마다 달라지는 설정도 번들에 담을 수 있다."""

    _use_bundle(
        monkeypatch,
        {
            "APP_SERVER_API_URL": "https://bundle.example.com/s/api/v1",
            "APP_SERVER_TIMEOUT_SEC": "7",
        },
    )

    settings = Settings()
    assert settings.app_server_api_url == "https://bundle.example.com/s/api/v1"
    assert settings.app_server_timeout_sec == 7.0


def test_key_absent_from_bundle_falls_through(monkeypatch):
    """번들에 없는 키는 환경변수 → `.env` → config.py 기본값으로 내려간다."""

    _use_bundle(monkeypatch, {"BEDROCK_MODEL": "from-bundle"})
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")

    settings = Settings()
    assert settings.bedrock_region == "us-east-1"  # 환경변수
    assert settings.repair_max_iterations == 3  # config.py 기본값


def test_bundle_name_itself_is_bootstrap_only(monkeypatch):
    """어느 번들을 읽을지는 번들이 정할 수 없다."""

    client = _use_bundle(monkeypatch, {"SECRETS_BUNDLE_NAME": "다른-번들"})

    Settings()

    assert client.secret_ids == [_BUNDLE]


def test_bundle_is_fetched_once(monkeypatch):
    """조회는 번들당 1회다. Settings 를 여러 번 만들어도 늘지 않는다."""

    client = _use_bundle(monkeypatch, {"BEDROCK_MODEL": "from-bundle"})

    Settings()
    Settings()

    assert client.calls == 1


def test_fetch_failure_falls_back_and_is_reported(monkeypatch, caplog):
    """조회에 실패해도 설정은 만들어진다. 실패는 기동 뒤 1408 로 보고한다."""

    _use_bundle(monkeypatch, error=RuntimeError("boom"))
    monkeypatch.setenv("BEDROCK_MODEL", "from-env")

    assert Settings().bedrock_model == "from-env"

    failure = secret_bundle.last_error()
    assert failure is not None and failure[0] == _BUNDLE

    with caplog.at_level(logging.WARNING, logger=secrets_module.logger.name):
        secrets_module.prefetch_secrets()

    record = _diagnostic(caplog)
    assert record.fields["errorCode"] == int(ErrorCode.SECRET_RESOLUTION_FAILED)
    assert record.fields["errorType"] == "RuntimeError"


def test_non_object_bundle_is_reported(monkeypatch, caplog):
    """번들이 JSON 객체가 아니면 같은 흡수 경로로 간다."""

    client = _FakeSecretsManager('"not-an-object"')
    monkeypatch.setenv(secret_bundle.BUNDLE_NAME_ENV, _BUNDLE)
    monkeypatch.setattr(secret_bundle, "_secrets_manager_client", lambda: client)

    Settings()

    with caplog.at_level(logging.WARNING, logger=secrets_module.logger.name):
        secrets_module.prefetch_secrets()

    assert _diagnostic(caplog).fields["errorCode"] == int(
        ErrorCode.SECRET_RESOLUTION_FAILED
    )


def test_resolve_secret_finds_keys_without_settings_fields(monkeypatch):
    """설정 필드가 없는 provider 키도 번들에서 온다."""

    _use_bundle(monkeypatch, {"ANTHROPIC_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", _BUNDLE)

    assert secrets_module.resolve_secret("anthropic_api_key") == "sk-bundle"
    assert secrets_module.resolve_secret("ANTHROPIC-API-KEY") == "sk-bundle"


def test_provider_takes_key_from_bundle(monkeypatch):
    """provider 는 키가 어디서 왔는지 모른다 — 해석기 한 곳만 본다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", _BUNDLE)
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")
    monkeypatch.setattr(secrets_module.settings, "openai_model", "gpt-test")

    assert OpenAIProvider().api_key == "sk-bundle"


def test_provider_error_message_mentions_both_sources(monkeypatch):
    """키가 어디에도 없으면 두 곳을 다 알려준다."""

    _use_bundle(monkeypatch, {})
    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", _BUNDLE)
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")
    monkeypatch.setattr(secrets_module.settings, "openai_model", "gpt-test")

    with pytest.raises(ValueError, match="시크릿 번들"):
        OpenAIProvider()


def test_bundle_values_never_appear_in_logs(monkeypatch, caplog):
    """값은 어떤 로그에도 남지 않는다. 이름과 개수만 남는다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle-secret"})
    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", _BUNDLE)

    with caplog.at_level(logging.INFO, logger=secrets_module.logger.name):
        secrets_module.prefetch_secrets()

    assert "sk-bundle-secret" not in caplog.text
    assert _diagnostic(caplog).fields["secretNameCount"] == 1
