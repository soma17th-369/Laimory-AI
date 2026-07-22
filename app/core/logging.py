"""관찰(observability) 로그 설정.

로컬에서는 rich 핸들러로, 운영에서는 stdout JSON 으로 초기화한다. 포맷은
`settings.log_format`("rich" | "json") 으로 고른다. 운영(AgentCore 서버리스)에서는
컨테이너 stdout/stderr 가 자동으로 CloudWatch Logs 로 수집되므로, JSON 라인으로
남기면 CloudWatch Logs Insights 에서 필드(level/logger 등)로 조회할 수 있다.
`get_logger` 를 통해 모듈별 로거를 가져다 쓴다.
"""

import json
import logging
import sys

from rich.logging import RichHandler

from app.core.config import settings

# 로깅이 중복 초기화되지 않도록 하는 플래그
_configured = False


class _JsonLogFormatter(logging.Formatter):
    """로그 레코드를 한 줄 JSON 으로 직렬화한다(CloudWatch Logs Insights 용)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """루트 로거를 초기화한다(포맷은 `settings.log_format`).

    이미 초기화된 경우 아무 작업도 하지 않는다.

    Args:
        level: 로그 레벨. 지정하지 않으면 설정값(`settings.log_level`)을 사용한다.
    """

    global _configured
    if _configured:
        return

    log_level = (level or settings.log_level).upper()

    if settings.log_format.lower() == "json":
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonLogFormatter())
        logging.basicConfig(level=log_level, handlers=[handler])
    else:
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
