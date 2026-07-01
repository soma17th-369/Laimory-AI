"""애플리케이션 공통 설정.

`.env` 파일과 환경 변수에서 값을 읽어 pydantic-settings 로 관리한다.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서버 전역에서 사용하는 설정 값.

    환경 변수 또는 프로젝트 루트의 `.env` 파일에서 값을 읽는다.
    필드 이름은 대소문자를 구분하지 않으므로 `OPENAI_API_KEY` 와 같은
    환경 변수는 `openai_api_key` 필드에 매핑된다.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 실행 환경 (local / dev / prod 등)
    app_env: str

    # 로그 레벨 (DEBUG / INFO / WARNING / ERROR)
    log_level: str

    # 사용할 LLM provider (openai | gemini | ... )
    llm_provider: str

    # provider 별 자격 증명/모델.
    # 실제로 사용하는 provider 것만 채우면 되므로 기본값은 빈 값("")이며,
    # 값이 비어 있는지에 대한 검증은 해당 provider 를 생성하는 시점(app/core/llm.py)에서 한다.
    # 새 provider 를 추가할 때는 `{provider}_api_key`, `{provider}_model` 규칙으로 필드를 추가한다.
    openai_api_key: str = ""
    openai_model: str = ""

    gemini_api_key: str = ""
    gemini_model: str = ""


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다.

    `lru_cache` 로 감싸 `.env` 파싱이 프로세스당 한 번만 일어나도록 한다.
    """

    return Settings()


settings = get_settings()
