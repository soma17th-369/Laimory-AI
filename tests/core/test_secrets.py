"""시크릿 해석 단위 테스트(이슈 #30).

환경변수 우선, 번들 fallback, 1회 캐시, 조회 실패 흡수, 평문 잔존 경고를 확인한다.
실제 AWS 는 호출하지 않는다.
"""

import json
import logging

import pytest
from pydantic import SecretStr

from app.core import secrets as secrets_module
from app.core.error_codes import ErrorCode
from app.core.llm import OpenAIProvider


@pytest.fixture(autouse=True)
def _clear_bundle_cache():
    secrets_module.reset_secret_cache()
    yield
    secrets_module.reset_secret_cache()


class _FakeSecretsManager:
    """`get_secret_value` 만 흉내 내는 테스트 더블."""

    def __init__(self, payload: str | None = None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls = 0
        self.secret_ids: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
        self.calls += 1
        self.secret_ids.append(SecretId)
        if self._error is not None:
            raise self._error
        return {"SecretString": self._payload or ""}


def _use_bundle(monkeypatch, mapping=None, *, name="laimory/test", error=None):
    payload = json.dumps(mapping) if mapping is not None else ""
    client = _FakeSecretsManager(payload, error)
    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", name)
    monkeypatch.setattr(secrets_module, "_secrets_manager_client", lambda: client)
    return client


def test_empty_bundle_name_never_calls_aws(monkeypatch):
    """번들 이름이 없으면 AWS 를 부르지 않는다. 로컬 동작이 예전과 같아야 한다."""

    def _forbidden():
        raise AssertionError("번들 이름이 없으면 AWS 를 호출하면 안 된다.")

    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", "")
    monkeypatch.setattr(secrets_module, "_secrets_manager_client", _forbidden)
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "sk-local")

    assert secrets_module.resolve_secret("OPENAI_API_KEY") == "sk-local"


def test_environment_value_wins_over_bundle(monkeypatch):
    """로컬에서 키 하나만 덮어써 실험하는 흐름을 지킨다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "sk-env")

    assert secrets_module.resolve_secret("openai_api_key") == "sk-env"


def test_bundle_fills_value_missing_from_environment(monkeypatch):
    """번들 키 표기는 `.env` 와 같은 이름이면 되고 대소문자를 가리지 않는다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")

    assert secrets_module.resolve_secret("openai_api_key") == "sk-bundle"
    assert secrets_module.resolve_secret("OPENAI-API-KEY") == "sk-bundle"


def test_secretstr_setting_is_unwrapped(monkeypatch):
    """`SecretStr` 필드도 같은 해석기를 지난다(Langfuse 키)."""

    _use_bundle(monkeypatch, {"LANGFUSE_SECRET_KEY": "sk-bundle"})
    monkeypatch.setattr(
        secrets_module.settings, "langfuse_secret_key", SecretStr("sk-env")
    )

    assert secrets_module.resolve_secret("langfuse_secret_key") == "sk-env"


def test_key_absent_from_bundle_is_empty(monkeypatch):
    """번들에 없는 키는 지금처럼 빈 값이다. 대상 목록을 코드가 갖지 않는다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "gemini_api_key", "")

    assert secrets_module.resolve_secret("gemini_api_key") == ""


def test_bundle_is_fetched_once(monkeypatch):
    """조회는 프로세스당 1회다. LLM 호출마다 AWS 를 부르지 않는다."""

    client = _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")

    secrets_module.resolve_secret("openai_api_key")
    secrets_module.resolve_secret("openai_api_key")
    secrets_module.load_secret_bundle()

    assert client.calls == 1
    assert client.secret_ids == ["laimory/test"]


def test_fetch_failure_is_absorbed_and_retried(monkeypatch, caplog):
    """조회 실패는 1408 로 남기고 빈 값으로 진행한다. 실패는 캐시하지 않는다."""

    _use_bundle(monkeypatch, error=RuntimeError("boom"))
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")

    with caplog.at_level(logging.WARNING, logger=secrets_module.logger.name):
        assert secrets_module.resolve_secret("openai_api_key") == ""

    record = caplog.records[-1]
    assert record.fields["errorCode"] == int(ErrorCode.SECRET_RESOLUTION_FAILED)
    assert record.fields["errorType"] == "RuntimeError"

    # 일시적 오류였다면 다음 호출에서 회복된다.
    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    assert secrets_module.resolve_secret("openai_api_key") == "sk-bundle"


def test_non_object_bundle_is_reported(monkeypatch, caplog):
    """번들이 JSON 객체가 아니면 같은 흡수 경로로 간다."""

    client = _FakeSecretsManager('"not-an-object"')
    monkeypatch.setattr(secrets_module.settings, "secrets_bundle_name", "laimory/test")
    monkeypatch.setattr(secrets_module, "_secrets_manager_client", lambda: client)

    with caplog.at_level(logging.WARNING, logger=secrets_module.logger.name):
        assert secrets_module.load_secret_bundle() == {}

    assert caplog.records[-1].fields["errorCode"] == int(
        ErrorCode.SECRET_RESOLUTION_FAILED
    )


def test_prefetch_warns_when_environment_shadows_bundle(monkeypatch, caplog):
    """배포 환경에 옛 평문이 남아 있으면 번들을 고쳐도 반영되지 않는다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "sk-env")
    monkeypatch.setattr(secrets_module.settings, "app_env", "prod")

    with caplog.at_level(logging.WARNING, logger=secrets_module.logger.name):
        secrets_module.prefetch_secrets()

    record = caplog.records[-1]
    assert record.fields["shadowedSecretNames"] == ["openai_api_key"]
    assert "sk-env" not in caplog.text
    assert "sk-bundle" not in caplog.text


def test_prefetch_is_quiet_in_local(monkeypatch, caplog):
    """로컬에서 `.env` 로 덮어쓰는 것은 정상이라 경고하지 않는다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "sk-env")
    monkeypatch.setattr(secrets_module.settings, "app_env", "local")

    with caplog.at_level(logging.WARNING, logger=secrets_module.logger.name):
        secrets_module.prefetch_secrets()

    assert caplog.records == []


def test_provider_takes_key_from_bundle(monkeypatch):
    """provider 는 키가 어디서 왔는지 모른다 — 해석기 한 곳만 본다."""

    _use_bundle(monkeypatch, {"OPENAI_API_KEY": "sk-bundle"})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")
    monkeypatch.setattr(secrets_module.settings, "openai_model", "gpt-test")

    assert OpenAIProvider().api_key == "sk-bundle"


def test_provider_error_message_mentions_both_sources(monkeypatch):
    """키가 어디에도 없으면 두 곳을 다 알려준다."""

    _use_bundle(monkeypatch, {})
    monkeypatch.setattr(secrets_module.settings, "openai_api_key", "")
    monkeypatch.setattr(secrets_module.settings, "openai_model", "gpt-test")

    with pytest.raises(ValueError, match="시크릿 번들"):
        OpenAIProvider()
