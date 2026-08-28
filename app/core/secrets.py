"""시크릿 조회 진입점(이슈 #30).

값의 정본은 **시크릿 번들**이다. 번들은 `Settings` 의 값 공급원으로 붙어 있어
(`app/core/secret_bundle.py`) 환경변수·`.env` 보다 우선하므로, 대부분의 값은 그냥
`settings.<필드>` 로 읽으면 된다. 이 모듈이 하는 일은 둘이다.

1. :func:`resolve_secret` — `Settings` 에 필드가 없는 키까지 포함해 이름으로 조회한다.
   provider 를 새로 추가해도(`{provider}_api_key`) 설정 필드를 만들지 않고 쓸 수 있다.
2. :func:`prefetch_secrets` — 기동 시 번들 로드 결과를 로그로 남긴다. 번들 조회는
   `Settings` 가 만들어질 때(import 시점) 이미 끝나 있고, 그때는 로깅 설정 전이라
   조용히 붙잡아 둔 실패를 여기서 1408 로 보고한다.

값이 어디서 왔는지는 호출부가 알지 않는다. 값 자체는 로그·예외 어디에도 남기지 않고
키 이름만 남긴다.

**환경 구성(운영 합의)**

- dev: EC2 `runtime.env` + 시크릿 번들
- prod: AgentCore Runtime + 시크릿 번들
- 그 밖의 고정값: `app/core/config.py` 기본값
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from app.core import secret_bundle
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger, log_fields
from app.core.operational_logging import DegradedComponent

logger = get_logger(__name__)


def _setting_value(name: str) -> str:
    """`Settings` 가 가진 값. 번들 → 환경변수 → `.env` → 기본값 순으로 이미 정해져 있다."""

    value: Any = getattr(settings, secret_bundle.normalize_key(name), "")
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return str(value or "")


def resolve_secret(name: str) -> str:
    """이름으로 값을 조회한다. `Settings` 필드가 아니면 번들에서 직접 찾는다."""

    value = _setting_value(name)
    if value:
        return value

    bundle = secret_bundle.bundle_values(settings.secrets_bundle_name)
    return bundle.get(secret_bundle.normalize_key(name), "")


def prefetch_secrets() -> None:
    """기동 시 1회 호출한다. 번들 로드 결과를 남기고 실패를 1408 로 보고한다."""

    failure = secret_bundle.last_error()
    if failure is not None:
        bundle_name, exc = failure
        report_error(
            logger,
            ErrorCode.SECRET_RESOLUTION_FAILED,
            "시크릿 번들을 읽지 못했습니다",
            exc=exc,
            context={"secretBundle": bundle_name},
            # 기동 시점이라 실행 컨텍스트가 없다. 단계가 아니라 프로세스 수준 결함이라
            # component 를 직접 준다 — 번들 없이 환경변수·기본값으로 떠 있는 상태다.
            component=DegradedComponent.SECRET_BUNDLE,
        )
        return

    bundle_name = settings.secrets_bundle_name.strip()
    if not bundle_name:
        return

    logger.info(
        "시크릿 번들 로드 완료",
        extra=log_fields(
            secretBundle=bundle_name,
            secretNameCount=len(secret_bundle.bundle_values(bundle_name)),
        ),
    )


def reset_secret_cache() -> None:
    """번들 캐시를 비운다. 테스트 전용."""

    secret_bundle.reset_cache()
