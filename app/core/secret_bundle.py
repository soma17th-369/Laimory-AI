"""시크릿 번들 로딩과 설정 소스(이슈 #30).

AWS Secrets Manager 시크릿 **하나**(JSON 객체)를 읽어 pydantic-settings 의 값 공급원으로
내놓는다. 번들은 `Settings` 보다 먼저 필요하므로 이 모듈은 **`app.core` 의 다른 모듈을
import 하지 않는다** — 설정이 만들어지기 전에 로거·예외 계층을 끌어오면 import 순환이 된다.

그래서 조회 실패도 여기서 붙잡아만 두고, 로깅이 준비된 뒤
:func:`app.core.secrets.prefetch_secrets` 가 1408 로 보고한다. 기동 시점의 예외를 그대로
띄우면 로그 포맷이 잡히기 전이라 Filebeat 가 구조화하지 못한다.

번들 이름은 `SECRETS_BUNDLE_NAME` 환경변수(또는 `.env`)로 온다. **비어 있으면 AWS 를
호출하지 않는다** — 로컬과 테스트가 AWS 를 건드리지 않는 것은 이 성질에 기댄다.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

#: 번들 이름을 담는 환경변수 이름.
BUNDLE_NAME_ENV = "SECRETS_BUNDLE_NAME"

#: 번들 이름 → 정규화된 {키: 값}. 프로세스 수명 동안 캐시한다.
_cache: dict[str, dict[str, str]] = {}

#: 마지막 조회 실패 (번들 이름, 예외). 성공하면 지운다.
_last_error: tuple[str, Exception] | None = None


def normalize_key(name: str) -> str:
    """키 이름을 설정 필드 이름 규칙으로 정규화한다(``OPENAI_API_KEY`` → ``openai_api_key``)."""

    return name.strip().lower().replace("-", "_")


def _secrets_manager_client():
    """AWS Secrets Manager 클라이언트. 번들 이름이 있을 때만 만든다.

    boto3 import 를 함수 안에 두어 번들을 쓰지 않는 실행이 AWS SDK 초기화 비용을 내지
    않게 한다. 리전과 자격증명은 boto3 기본 체인이 정한다 — EC2 Instance Role 과
    AgentCore Runtime 실행 역할이 그대로 쓰이고 별도 키를 두지 않는다.
    """

    import boto3

    return boto3.client("secretsmanager")


def _fetch(bundle_name: str) -> dict[str, str]:
    """번들 하나를 읽어 정규화한 ``{키: 값}`` 으로 돌려준다."""

    response = _secrets_manager_client().get_secret_value(SecretId=bundle_name)
    raw = response.get("SecretString") or ""
    if not raw.strip():
        return {}

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("시크릿 번들이 JSON 객체가 아닙니다.")

    return {
        normalize_key(key): str(value)
        for key, value in parsed.items()
        if isinstance(key, str) and value is not None
    }


def load_bundle(bundle_name: str) -> dict[str, str]:
    """번들을 한 번 읽어 캐시하고 돌려준다. 실패하면 빈 dict 이고 오류를 기록해 둔다."""

    global _last_error

    key = (bundle_name or "").strip()
    if not key:
        return {}
    if key in _cache:
        return _cache[key]

    try:
        loaded = _fetch(key)
    except Exception as exc:  # noqa: BLE001 - 실패는 여기서 삼키고 나중에 보고한다.
        _last_error = (key, exc)
        return {}

    _cache[key] = loaded
    _last_error = None
    return loaded


def bundle_values(bundle_name: str) -> dict[str, str]:
    """이미 읽어 둔 번들 값. 읽지 않았으면 그때 읽는다."""

    return load_bundle(bundle_name)


def last_error() -> tuple[str, Exception] | None:
    """마지막 조회 실패 (번들 이름, 예외). 없으면 ``None``."""

    return _last_error


def reset_cache() -> None:
    """번들 캐시와 오류 기록을 비운다. 테스트 전용."""

    global _last_error
    _cache.clear()
    _last_error = None


class SecretBundleSettingsSource(PydanticBaseSettingsSource):
    """번들을 `Settings` 의 값 공급원으로 붙이는 소스.

    `Settings` 가 만들어질 때 한 번 호출된다. 여기서 돌려준 키는 환경변수·`.env` 보다
    **우선**한다 — 정본을 하나로 두기 위해서다(#30). 번들에 없는 키는 그대로 환경변수와
    `.env`, 그다음 `config.py` 의 기본값으로 내려간다.
    """

    def __init__(self, settings_cls, dotenv_settings: PydanticBaseSettingsSource) -> None:
        super().__init__(settings_cls)
        self._dotenv_settings = dotenv_settings

    def bundle_name(self) -> str:
        """번들 이름. 환경변수가 먼저이고 없으면 `.env` 를 본다.

        이 값만은 번들에서 올 수 없다 — 어느 번들을 읽을지 정하는 부트스트랩 값이다.
        """

        for candidate in (BUNDLE_NAME_ENV, BUNDLE_NAME_ENV.lower()):
            value = os.environ.get(candidate, "")
            if value.strip():
                return value.strip()

        try:
            dotenv_values = self._dotenv_settings()
        except Exception:  # noqa: BLE001 - `.env` 가 없거나 깨져도 번들만 건너뛴다.
            return ""

        raw = dotenv_values.get(BUNDLE_NAME_ENV.lower()) or dotenv_values.get(
            BUNDLE_NAME_ENV
        )
        return str(raw or "").strip()

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # 값은 `__call__` 이 한 번에 돌려주므로 필드 단위 조회는 쓰지 않는다.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(load_bundle(self.bundle_name()))
