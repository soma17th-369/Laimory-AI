"""관찰(observability) 로그 설정.

rich 핸들러 기반으로 애플리케이션 로깅을 초기화한다.
`get_logger` 를 통해 모듈별 로거를 가져다 쓴다.
"""

import logging

from rich.logging import RichHandler

from app.core.config import settings

# 로깅이 중복 초기화되지 않도록 하는 플래그
_configured = False


def configure_logging(level: str | None = None) -> None:
    """루트 로거를 rich 핸들러로 초기화한다.

    이미 초기화된 경우 아무 작업도 하지 않는다.

    Args:
        level: 로그 레벨. 지정하지 않으면 설정값(`settings.log_level`)을 사용한다.
    """

    global _configured
    if _configured:
        return

    log_level = (level or settings.log_level).upper()

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """이름이 지정된 로거를 반환한다.

    최초 호출 시 로깅 설정이 초기화된다.

    Args:
        name: 보통 `__name__` 을 넘긴다.
    """

    configure_logging()
    return logging.getLogger(name)
