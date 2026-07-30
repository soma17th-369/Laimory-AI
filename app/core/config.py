"""애플리케이션 공통 설정.

`.env` 파일과 환경 변수에서 값을 읽어 pydantic-settings 로 관리한다.
"""

import re
import tomllib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_agent_version() -> str:
    try:
        return version("laimory-ai")
    except PackageNotFoundError:
        try:
            pyproject = tomllib.loads(
                (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )
            return str(pyproject["project"]["version"])
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return "unknown"


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
    # 실제 자격증명은 ~/.aws/credentials 에 둔다. 배포 시 프로필을 비우면 EC2
    # Instance Role 또는 AgentCore Runtime 실행 역할의 임시 자격증명을 자동 사용한다.
    bedrock_aws_profile: str = ""
    # model 은 Nova 모델 id 또는 크로스리전 추론 프로필 id. Nova 2 Lite 는 서울에서
    # Global inference profile(`global.` 접두)로 호출하며, 서울 리전 자격증명으로
    # 실제 호출까지 확인했다. 모델 목록 조회(bedrock:ListFoundationModels)는 별개
    # 권한이라 없어도 호출에는 지장이 없다.
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

    # App Server 서버간 API 기본 URL. `/s/api/{version}` 까지를 넣고, task별
    # Timeline callback 리소스 경로는 코드에서 조립한다. 요청 계약에는 callback URL이
    # 없으므로 환경별로 고정하며, 비어 있으면 완료 콜백을 보내지 않는다.
    app_server_api_url: str | None = None

    # App Server 콜백 POST 요청 timeout(초).
    callback_timeout_sec: float = 10.0

    @field_validator("app_server_api_url", mode="before")
    @classmethod
    def validate_app_server_api_url(cls, value: object) -> str | None:
        """App Server API 기본 URL을 정규화하고 절대 HTTP(S) URL인지 검증한다."""

        if value is None:
            return None

        normalized = str(value).strip()
        if not normalized:
            return None

        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("APP_SERVER_API_URL은 절대 HTTP(S) URL이어야 합니다.")
        if parsed.query or parsed.fragment:
            raise ValueError("APP_SERVER_API_URL에는 query 또는 fragment를 넣을 수 없습니다.")
        if re.search(r"/s/api/v\d+/?$", parsed.path) is None:
            raise ValueError("APP_SERVER_API_URL은 /s/api/v{숫자} 경로로 끝나야 합니다.")

        return normalized.rstrip("/")

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

    # --- 관측(observability) export 설정 (이슈 #28) ---
    # Timeline 실행 로그를 taskId 단위 JSON 문서로 Elasticsearch 에 보낸다. 마스터
    # 스위치가 꺼져 있고 로컬 출력도 없으면 수집 자체를 하지 않는다. 관측은 부가
    # 기능이라 꺼져도, 전송에 실패해도 Timeline 처리에는 영향을 주지 않는다.
    obs_enabled: bool = False
    # 배포/빌드 버전. 관측 이벤트/문서에 agentVersion 으로 실어 버전별 품질 비교에 쓴다.
    agent_version: str = _default_agent_version()
    # Elasticsearch 접속. es_url 이 비어 있으면 ES 전송을 건너뛴다.
    es_url: str = ""
    es_api_key: str = ""
    es_event_index: str = "ai-timeline-task"
    es_timeout_sec: float = 5.0
    es_max_retries: int = 3
    # SANITIZED 는 입력·프롬프트·응답·draft 등 실행 본문을 마스킹한 뒤 저장하고,
    # NONE 은 본문 대신 길이와 해시만 남긴다. 어느 정책이든 이벤트당 크기 제한을
    # 적용하며 마스킹하지 않은 원문 저장 모드는 제공하지 않는다.
    obs_content_capture: Literal["NONE", "SANITIZED"] = "SANITIZED"
    obs_max_payload_bytes: int = 256 * 1024
    obs_max_events_per_task: int = 1000
    # dev 검사용: 값이 있으면 조립한 event 문서를 로컬에도 저장한다.
    obs_local_dir: str | None = None

    # --- Langfuse tracing ---
    # 기존 Elasticsearch 관측을 대체하지 않는 선택적 외부 tracing 계층이다.
    # 키가 없거나 비활성화돼 있으면 no-op이며 Timeline 처리를 실패시키지 않는다.
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    # 이 프로젝트의 POC/운영 대상은 Langfuse Cloud Japan 리전이다.
    langfuse_base_url: str = "https://jp.cloud.langfuse.com"
    langfuse_sample_rate: float = 1.0
    # 위치·건강·캘린더처럼 민감한 본문은 기본적으로 외부에 보내지 않는다.
    langfuse_content_capture: Literal["NONE", "SANITIZED"] = "NONE"
    langfuse_max_payload_bytes: int = 64 * 1024

    @field_validator("langfuse_sample_rate")
    @classmethod
    def validate_langfuse_sample_rate(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("LANGFUSE_SAMPLE_RATE는 0과 1 사이여야 합니다.")
        return value

    @field_validator("langfuse_base_url", mode="before")
    @classmethod
    def validate_langfuse_base_url(cls, value: object) -> str:
        normalized = str(value).strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LANGFUSE_BASE_URL은 절대 HTTP(S) URL이어야 합니다.")
        if parsed.query or parsed.fragment:
            raise ValueError("LANGFUSE_BASE_URL에는 query 또는 fragment를 넣을 수 없습니다.")
        return normalized

    @field_validator("langfuse_max_payload_bytes")
    @classmethod
    def validate_langfuse_max_payload_bytes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("LANGFUSE_MAX_PAYLOAD_BYTES는 1 이상이어야 합니다.")
        return value

    # 운영 로그 포맷: "rich"(로컬 콘솔) | "json"(stdout JSON, CloudWatch Logs Insights).
    log_format: str = "rich"


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다.

    `lru_cache` 로 감싸 `.env` 파싱이 프로세스당 한 번만 일어나도록 한다.
    """

    return Settings()


settings = get_settings()
