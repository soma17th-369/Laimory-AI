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

# Langfuse 로 실행 본문을 내보내도 되는 실행 환경(이슈 #48). 개발 중에는 프롬프트와
# 중간 산출물을 봐야 디버깅이 되고, 그 밖에서는 기본적으로 내보내지 않는다.
_BODY_VISIBLE_APP_ENVS = {"local", "dev"}


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

    # 모든 Agent가 함께 사용할 프롬프트 세트 버전. 버전별 파일은 각 Agent의
    # `prompts/{version}/` 아래에 두며, 일부 Agent만 다른 버전을 쓰지 않는다.
    prompt_version: Literal["v1", "v2"] = "v1"

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

    # 외부 시크릿 번들(#30). AWS Secrets Manager 시크릿 하나의 이름 또는 ARN 이며, 값은
    # `{"OPENAI_API_KEY": "...", "LANGFUSE_SECRET_KEY": "..."}` 형태의 JSON 객체다.
    #
    # **비어 있으면 AWS 를 호출하지 않는다.** 로컬은 지금처럼 `.env` 만 보고, 배포 환경에서만
    # 이 이름을 주입한다. 어떤 키가 번들에 드는지는 코드가 알지 않는다 — 있는 키를 쓰고
    # 없으면 비운다(app/core/secrets.py). 시크릿 **값** 은 어디에도 커밋하지 않는다.
    secrets_bundle_name: str = ""

    # --- 사진 이미지 다운로드 (이슈 #52) ---
    # App Server 가 PHOTO payload 에 실어 주는 `photoUrl` 에서 실제 이미지를 내려받아
    # vision 모델에 넘기기 위한 정책이다. 장당 크기·형식·장수 기본값은 App Server 의
    # 업로드 제한(장당 5MB, 요청당 20장, JPEG/PNG/WebP)과 맞춘다.

    #: 이미지 하나를 받는 데 허용할 시간(초).
    photo_download_timeout_sec: float = 5.0

    #: 이미지 한 장의 최대 크기. App Server 업로드 상한과 같다. 정확히 이 값인 파일은
    #: 통과해야 하므로 **초과**일 때만 거부한다.
    photo_max_image_bytes: int = 5 * 1024 * 1024

    #: 한 번에 내려받을 최대 장수. App Server 요청당 상한과 같다.
    photo_max_images: int = 20

    #: 한 배치에서 받을 이미지 bytes 총합 상한. **AI 서버 고유 제한이다.**
    #: App Server 는 총합을 제한하지 않아 이론상 20장 × 5MB = 100MB 가 가능한데,
    #: describer 가 배치 1회 vision 호출이라 그 전부가 한 요청에 실린다. base64 로
    #: 약 1.33배 부풀어 provider 요청 한도를 넘고 토큰 비용도 함께 뛴다.
    photo_max_total_image_bytes: int = 20 * 1024 * 1024

    #: 이미지를 동시에 받을 스레드 수.
    photo_download_max_workers: int = 4

    #: 배치 전체 다운로드에 허용할 시간(초). 넘기면 못 받은 사진은 fallback 으로 간다.
    photo_download_budget_sec: float = 30.0

    @field_validator(
        "photo_max_image_bytes",
        "photo_max_images",
        "photo_max_total_image_bytes",
        "photo_download_max_workers",
    )
    @classmethod
    def validate_photo_positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("사진 다운로드 상한 설정은 1 이상이어야 합니다.")
        return value

    @field_validator("photo_download_timeout_sec", "photo_download_budget_sec")
    @classmethod
    def validate_photo_positive_sec(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("사진 다운로드 시간 설정은 0보다 커야 합니다.")
        return value

    # 타임라인 메인 에이전트 전체 실행 timeout(초). 초과 시 task 를 FAILED 로 처리한다.
    # Repair Agent 가 분석·개선을 반복하며 LLM 을 여러 번 부르므로 넉넉히 잡는다.
    pipeline_timeout_sec: float = 120.0

    # Repair Agent 의 분석-개선 반복 상한. 한 번의 반복이 LLM 호출 1회다.
    # 0 이면 LLM 개선 없이 결정론 확정(draft_repair)만 수행한다.
    repair_max_iterations: int = 3

    # User Memory 갱신(#64) 전체 실행 timeout(초). 초과해도 FAILED 결과는 보낸다.
    #
    # 상한이 필요한 이유는 App Server TTL(3분) 때문만이 아니다. `app/core/llm.py` 에
    # 자체 timeout 이 없어 provider SDK 기본값(OpenAI 600초)을 따르는데, 그동안
    # `GET /ping` 이 HealthyBusy 를 답해 **컨테이너 회수와 배포가 막힌다**.
    # 갱신은 LLM 호출 1~3회라 타임라인보다 짧게 잡는다.
    user_memory_timeout_sec: float = 120.0

    # App Server 서버간 API 기본 URL. 버전 경로(`/s/api/v1` 또는 `/s/v1`)까지를
    # 넣고, task별 리소스 경로(input/result/callback)는 코드에서 조립한다. 요청
    # 계약에는 URL이 없으므로 환경별로 고정한다.
    #
    # **필수**다(이슈 #40). AI 서버는 더 이상 staging DB 에 직접 붙지 않고 입력
    # 조회·결과 저장·완료 통보를 전부 이 API 로 한다 — 값이 없으면 아무 일도 할 수
    # 없으므로 기동 시점에 실패하는 편이 낫다.
    app_server_api_url: str

    # App Server API 요청 timeout(초). 입력 조회·결과 저장·콜백·User Memory 결과 저장에
    # 공통 적용한다.
    #
    # 값의 근거는 호출 1회가 아니라 재시도까지 합친 총예산이다(이슈 #86). timeout 장애에서
    # 3초 × 3회 시도 + 대기(0.5 + 1.0초) ≒ 10.5초에 최종 실패가 확정된다. 10초였을 때는
    # 같은 장애가 31.5초까지 매달렸다.
    # 정상 응답이 3초를 넘는 환경이라면 이 기본값이 아니라 APP_SERVER_TIMEOUT_SEC 를 올린다.
    app_server_timeout_sec: float = 3.0

    # App Server API 시도 횟수 상한(최초 1회 + 재시도). timeout/5xx 에만 재시도한다.
    # 4xx 는 재시도해도 같은 답이 오므로 즉시 중단한다.
    app_server_max_attempts: int = 3

    # 재시도 간 대기 시간(초). 시도마다 2배씩 늘린다(0.5 → 1.0 → 2.0).
    app_server_retry_backoff_sec: float = 0.5

    @field_validator("prompt_version", mode="before")
    @classmethod
    def normalize_prompt_version(cls, value: object) -> str:
        """프롬프트 버전 표기를 정규화한다. 지원 여부는 Literal 검증이 맡는다."""

        return str(value).strip().lower()

    @field_validator("app_server_api_url", mode="before")
    @classmethod
    def validate_app_server_api_url(cls, value: object) -> str:
        """App Server API 기본 URL을 정규화하고 절대 HTTP(S) URL인지 검증한다."""

        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("APP_SERVER_API_URL은 비어 있을 수 없습니다.")

        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("APP_SERVER_API_URL은 절대 HTTP(S) URL이어야 합니다.")
        if parsed.query or parsed.fragment:
            raise ValueError("APP_SERVER_API_URL에는 query 또는 fragment를 넣을 수 없습니다.")
        # 배포 환경마다 서버간 API 접두사가 `/s/api/v1` 과 `/s/v1` 로 갈린다. 둘 다
        # 받되 버전 경로가 빠진 값은 계속 막는다 — 버전 없는 base 로 붙으면 경로가
        # 통째로 404 가 되고, 그때는 원인이 설정이라는 게 잘 드러나지 않는다.
        if re.search(r"/s(/api)?/v\d+/?$", parsed.path) is None:
            raise ValueError(
                "APP_SERVER_API_URL은 /s/api/v{숫자} 또는 /s/v{숫자} 경로로 끝나야 합니다."
            )

        return normalized.rstrip("/")

    @field_validator("app_server_max_attempts")
    @classmethod
    def validate_app_server_max_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("APP_SERVER_MAX_ATTEMPTS는 1 이상이어야 합니다.")
        return value

    # 배포/빌드 버전. 운영 로그의 `version` 필드와 Langfuse release 로 나가 어느
    # 배포본이 낸 로그·trace 인지 특정한다. EC2 배포는 이미지 태그를 넣는다.
    agent_version: str = _default_agent_version()

    # --- Langfuse tracing ---
    # AI 실행 관측(agent 트리·LLM generation·토큰)을 전담하는 계층이다(이슈 #47).
    # 키가 없거나 비활성화돼 있으면 no-op이며 Timeline 처리를 실패시키지 않는다.
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    # 이 프로젝트의 POC/운영 대상은 Langfuse Cloud Japan 리전이다.
    langfuse_base_url: str = "https://jp.cloud.langfuse.com"
    langfuse_sample_rate: float = 1.0
    # 위치·건강·캘린더처럼 민감한 본문은 기본적으로 외부에 보내지 않는다. 값을 지정하지
    # 않으면 실행 환경으로 정한다(`langfuse_capture_policy` 참고).
    langfuse_content_capture: Literal["NONE", "SANITIZED"] | None = None
    langfuse_max_payload_bytes: int = 64 * 1024

    @property
    def langfuse_capture_policy(self) -> str:
        """실제로 적용할 Langfuse 콘텐츠 정책.

        `LANGFUSE_CONTENT_CAPTURE` 를 지정하면 그 값이 항상 이긴다. 지정하지 않으면
        local/dev 는 `SANITIZED`, 그 밖은 `NONE` 이다. 개발 환경에서 기본값이 `NONE`
        이라 trace 에 해시만 남아 디버깅이 불가능했다(이슈 #48).

        dev 컨테이너는 EC2 의 `/opt/laimory-ai/runtime.env` 를 읽으므로, 그 파일에
        `LANGFUSE_CONTENT_CAPTURE=NONE` 이 남아 있으면 이 기본값보다 우선한다.
        """

        if self.langfuse_content_capture is not None:
            return self.langfuse_content_capture
        if self.app_env.strip().lower() in _BODY_VISIBLE_APP_ENVS:
            return "SANITIZED"
        return "NONE"

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

    # 운영 로그 포맷: "rich"(로컬 콘솔) | "json"(stdout 한 줄 JSON).
    # 운영에서는 반드시 "json" 이다. EC2 에서 Filebeat 가 컨테이너 stdout 을 줄 단위로
    # 읽어 Elasticsearch 로 보내므로, 한 줄이 유효한 JSON 이 아니면 그 이벤트를 잃는다.
    log_format: str = "rich"


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다.

    `lru_cache` 로 감싸 `.env` 파싱이 프로세스당 한 번만 일어나도록 한다.
    """

    return Settings()


settings = get_settings()
