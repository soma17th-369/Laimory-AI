"""애플리케이션 공통 설정.

`.env` 파일과 환경 변수에서 값을 읽어 pydantic-settings 로 관리한다.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """서버 전역에서 사용하는 설정 값.

    환경 변수 또는 프로젝트 루트의 `.env` 파일에서 값을 읽는다.
    필드 이름은 대소문자를 구분하지 않으므로 `OPENAI_API_KEY` 와 같은
    환경 변수는 `openai_api_key` 필드에 매핑된다.
    """

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 실행 환경 (local / dev / prod 등)
    app_env: str

    # 로그 레벨 (DEBUG / INFO / WARNING / ERROR)
    log_level: str

    # 직접 실행 시 uvicorn 바인딩 설정
    server_host: str = "127.0.0.1"
    server_port: int = 8000

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

    # Amazon Bedrock (Nova 등). Bedrock 은 provider 별 api_key 가 아니라 AWS 자격증명
    # 체인으로 인증하므로 `bedrock_api_key` 는 없다. 로컬에서는 프로필 이름만 지정하고
    # 실제 자격증명은 ~/.aws/credentials 에 둔다. 배포 시 프로필을 비우면 AgentCore
    # Runtime 실행 역할의 임시 자격증명을 boto3 가 자동으로 사용한다.
    bedrock_aws_profile: str = ""
    # model 은 Nova 모델 id 또는 크로스리전 추론 프로필 id. Nova 2 Lite 는 서울에서
    # Global profile 을 사용하며, 조직 SCP가 대상 리전을 허용해야 호출할 수 있다.
    bedrock_region: str = "ap-northeast-2"
    bedrock_model: str = ""

    # 사진 vision describe 에서 실제 이미지를 읽어올 로컬 디렉터리.
    # 값이 있으면 `LocalFilePhotoImageSource` 로 해당 디렉터리의 실제 파일을 읽고,
    # 비어 있으면(기본) 이미지 없이 메타데이터 기반 fallback 으로 동작한다.
    # (S3 가 연결되면 별도 이미지 소스로 교체한다.)
    photo_image_dir: str | None = None

    # 타임라인 메인 에이전트 전체 실행 timeout(초). 초과 시 task 를 FAILED 로 처리한다.
    # Repair Agent 가 분석·개선을 반복하며 LLM 을 여러 번 부르므로 넉넉히 잡는다.
    pipeline_timeout_sec: float = 120.0

    # Repair Agent 의 분석-개선 반복 상한. 한 번의 반복이 LLM 호출 1회다.
    # 0 이면 LLM 개선 없이 결정론 확정(draft_repair)만 수행한다.
    repair_max_iterations: int = 3

    # 처리 완료 시 결과를 POST 로 되돌려줄 App Server 콜백 URL.
    # 새 입력 계약에는 요청 단위 callbackUrl 이 없어, 설정으로 고정한다.
    # 비어 있으면 완료 콜백을 보내지 않는다.
    callback_url: str | None = None

    # App Server 콜백 POST 요청 timeout(초).
    callback_timeout_sec: float = 10.0

    # --- staging RDB(MySQL) 설정 (이슈 #25) ---
    # AI 서버는 App Server 가 적재한 timeline_draft_source_items 를 taskId 로 읽고,
    # 분석 결과를 timeline_events/timeline_items 에 저장한다. DB 는 필수다 — DB 없이
    # 도는 모드는 없으며, 접속 정보가 없거나 접속이 안 되면 처리에 실패한다.
    # 접속은 host/port 직결이다. prod 는 VPC 로 private subnet DB 에 바로 붙고,
    # 로컬 검증은 SSH 터널을 열어 DB_HOST 를 127.0.0.1:로컬포트로 가리키면 된다.
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "laimory"
    db_user: str = ""
    db_password: str = ""
    # SQLAlchemy engine 이 실행 SQL 을 로그로 남길지(디버깅용).
    db_echo: bool = False
    # DB 작업(조회/저장) 요청 timeout(초).
    db_timeout_sec: float = 10.0


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다.

    `lru_cache` 로 감싸 `.env` 파싱이 프로세스당 한 번만 일어나도록 한다.
    """

    return Settings()


settings = get_settings()
