"""tests/core import 단계에서 필요한 기본 환경 변수 설정.

`app.core.config.Settings` 는 APP_ENV/LOG_LEVEL/LLM_PROVIDER 를 요구하므로,
`.env` 가 없는 환경(CI 등)에서도 import 가 되도록 기본값을 채운다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]

load_dotenv(ROOT_DIR / ".env", override=False)

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("LLM_PROVIDER", "openai")
