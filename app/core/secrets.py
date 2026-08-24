"""외부 시크릿 해석(이슈 #30).

시크릿 값을 읽는 자리를 한 곳으로 모은다. 해석 순서는 **환경변수/`.env` → AWS Secrets
Manager 번들 → 빈 문자열**이다.

번들은 시크릿 **하나**에 JSON 객체로 들어 있고(``{"OPENAI_API_KEY": "...", ...}``), 기동
시 1회 읽어 프로세스 수명 동안 캐시한다. 어떤 키가 들어 있는지는 코드가 알지 않는다 —
있는 키를 그대로 쓰고 없으면 비운다. 그래서 옮기는 키가 늘어도 이 파일은 바뀌지 않는다.
키 이름은 `.env` 와 같은 이름을 쓰며 대소문자와 하이픈을 가리지 않는다.

**환경변수가 번들보다 우선한다.** 로컬에서 키 하나만 덮어써 실험하는 흐름을 그대로 두기
위해서다. 대신 배포 환경에 옛 평문이 남아 있으면 번들 값을 고쳐도 반영되지 않으므로,
기동 시 그 상태를 찾아 경고한다(:func:`prefetch_secrets`). 이 경고가 "AWS 로 옮겼다고
믿는데 실은 예전 값으로 돌고 있는" 상태를 드러내는 유일한 장치다.

조회 실패는 기동을 막지 않는다. 빈 값으로 진행하고 그 키가 실제로 필요한 시점에
provider 생성이 실패한다 — 시크릿 하나 때문에 컨테이너가 뜨지 못하면 그것이 더 큰
장애다. 실패는 캐시하지 않으므로 일시적인 오류는 다음 호출에서 저절로 회복된다.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import SecretStr

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger, log_fields

logger = get_logger(__name__)

#: 평문 잔존을 경고할 실행 환경. local/dev 는 `.env` 로 덮어쓰는 것이 정상이라 조용하다.
_PLAINTEXT_GUARD_ENVS = {"prod", "production", "staging"}

#: 로드된 번들. ``None`` 은 "아직 읽지 않았다", ``{}`` 는 "읽었고 비어 있다"다.
_bundle: dict[str, str] | None = None


def _normalize(name: str) -> str:
    """키 이름을 비교용으로 정규화한다(``OPENAI_API_KEY`` → ``openai_api_key``)."""

    return name.strip().lower().replace("-", "_")


def _setting_value(name: str) -> str:
    """환경변수/`.env` 에서 온 값. 없으면 빈 문자열."""

    value: Any = getattr(settings, _normalize(name), "")
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return str(value or "")


def _secrets_manager_client():
    """AWS Secrets Manager 클라이언트. 번들 이름이 있을 때만 만든다.

    boto3 import 를 함수 안에 두어 번들을 쓰지 않는 실행(로컬·테스트)이 AWS SDK 초기화
    비용을 내지 않게 한다. 리전과 자격증명은 boto3 기본 체인이 정한다 — EC2 Instance
    Role 과 AgentCore Runtime 실행 역할이 그대로 쓰이고, 별도 키를 두지 않는다.
    """

    import boto3

    return boto3.client("secretsmanager")


def _fetch_bundle(bundle_name: str) -> dict[str, str]:
    """번들 하나를 읽어 정규화한 ``{키: 값}`` 으로 돌려준다."""

    response = _secrets_manager_client().get_secret_value(SecretId=bundle_name)
    raw = response.get("SecretString") or ""
    if not raw.strip():
        return {}

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("시크릿 번들이 JSON 객체가 아닙니다.")

    return {
        _normalize(key): str(value)
        for key, value in parsed.items()
        if isinstance(key, str) and value is not None
    }


def load_secret_bundle() -> dict[str, str]:
    """번들을 한 번 읽어 캐시하고 돌려준다.

    번들 이름이 비어 있으면 AWS 를 호출하지 않고 빈 dict 를 캐시한다. 조회 실패는
    캐시하지 않아 다음 호출에서 다시 시도한다.
    """

    global _bundle
    if _bundle is not None:
        return _bundle

    bundle_name = settings.secrets_bundle_name.strip()
    if not bundle_name:
        _bundle = {}
        return _bundle

    try:
        loaded = _fetch_bundle(bundle_name)
    except Exception as exc:  # noqa: BLE001 - 조회 실패가 기동을 막지 않는다.
        report_error(
            logger,
            ErrorCode.SECRET_RESOLUTION_FAILED,
            "시크릿 번들을 읽지 못했습니다",
            exc=exc,
            context={"secretBundle": bundle_name},
        )
        return {}

    _bundle = loaded
    logger.info(
        "시크릿 번들 로드 완료",
        extra=log_fields(secretBundle=bundle_name, secretNameCount=len(loaded)),
    )
    return _bundle


def resolve_secret(name: str) -> str:
    """시크릿 하나를 해석한다. 환경변수/`.env` → 번들 → 빈 문자열."""

    direct = _setting_value(name)
    if direct:
        return direct
    return load_secret_bundle().get(_normalize(name), "")


def shadowed_secret_names() -> list[str]:
    """번들에 있는데 환경변수가 덮어쓰고 있는 키 이름. 값은 다루지 않는다."""

    return sorted(name for name in load_secret_bundle() if _setting_value(name))


def prefetch_secrets() -> None:
    """기동 시 1회 호출한다. 번들을 미리 읽고 우선순위 문제를 경고한다."""

    if not load_secret_bundle():
        return

    shadowed = shadowed_secret_names()
    if shadowed and settings.app_env.strip().lower() in _PLAINTEXT_GUARD_ENVS:
        logger.warning(
            "환경변수 값이 시크릿 번들을 덮어쓰고 있습니다. 번들 값을 고쳐도 반영되지 "
            "않으므로 배포 환경에서 해당 환경변수를 제거하십시오.",
            extra=log_fields(shadowedSecretNames=shadowed),
        )


def reset_secret_cache() -> None:
    """번들 캐시를 비운다. 테스트 전용."""

    global _bundle
    _bundle = None
