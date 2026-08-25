# 런타임 설정값 구분

> 로컬 `.env`와 AgentCore 환경변수는 같은 설정 키를 같은 방식으로 읽는다.
> Secrets Manager에는 실제 비밀값만 넣는다.

## 1. `.env`·AgentCore 환경변수

비밀이 아닌 실행 설정은 로컬에서는 `.env`, production에서는 AgentCore Runtime의
`environmentVariables`에 넣는다.

| 변수 | 로컬 `.env` | AgentCore 환경변수 |
|---|---|---|
| `APP_ENV` | `local` | `prod` |
| `LOG_LEVEL` | `INFO` | `INFO` |
| `LOG_FORMAT` | `rich` | `json` |
| `SERVER_HOST` | `127.0.0.1` | 넣지 않음. Docker 실행 명령이 `0.0.0.0` 사용 |
| `SERVER_PORT` | `8000` | 넣지 않음. Docker 실행 명령이 `8080` 사용 |
| `LLM_PROVIDER` | `bedrock` | `bedrock` |
| `PROMPT_VERSION` | `v1` | `v1` |
| `OPENAI_MODEL` | OpenAI 사용 시 모델 ID | 현재 넣지 않음. OpenAI 사용 시 모델 ID |
| `GEMINI_MODEL` | Gemini 사용 시 모델 ID | 현재 넣지 않음. Gemini 사용 시 모델 ID |
| `BEDROCK_AWS_PROFILE` | 로컬 AWS profile 이름 | 넣지 않음 |
| `BEDROCK_REGION` | `ap-northeast-2` | `ap-northeast-2` |
| `BEDROCK_MODEL` | `global.amazon.nova-2-lite-v1:0` | `global.amazon.nova-2-lite-v1:0` |
| `APP_SERVER_API_URL` | 로컬 App Server URL | 같은 VPC의 App Server 내부 URL |
| `LANGFUSE_ENABLED` | 필요에 따라 `true`/`false` | `true` |
| `LANGFUSE_PUBLIC_KEY` | 로컬 프로젝트 public key | 운영 프로젝트 public key |
| `LANGFUSE_BASE_URL` | `https://jp.cloud.langfuse.com` | `https://jp.cloud.langfuse.com` |
| `LANGFUSE_CONTENT_CAPTURE` | 생략하면 `SANITIZED` | `NONE` |
| `SECRETS_BUNDLE_NAME` | 기본적으로 넣지 않음 | `laimory-ai/prod/app` |

AgentCore가 이 값을 컨테이너 프로세스 환경으로 주입하므로 애플리케이션은 `.env`와 동일하게
`pydantic-settings`로 읽는다. AgentCore 환경변수를 boto3로 다시 조회하지 않는다.

`SERVER_HOST`와 `SERVER_PORT`는 `python -m app.server`로 실행할 때만 사용한다. Docker와
AgentCore는 `uvicorn --host 0.0.0.0 --port 8080`으로 실행하므로 두 환경변수를 사용하지 않는다.

`AGENT_VERSION`은 넣지 않는다. AgentCore에서는 이미지에 설치된 패키지 버전을 사용한다.

로컬에서만 사용하는 API key와 `LANGFUSE_SECRET_KEY`는 gitignored `.env`에 둘 수 있다.
실제 값을 커밋하지 않는다.

## 2. Secrets Manager

Secrets Manager에 저장하는 비밀 변수 전체 목록은 다음과 같다.

| 변수 | 저장할 Secret | 현재 production 필수 | 사용 조건·용도 |
|---|---|:---:|---|
| `LANGFUSE_SECRET_KEY` | `laimory-ai/prod/app` | O | Langfuse 운영 프로젝트 인증 |
| `OPENAI_API_KEY` | `laimory-ai/prod/app` | X | `LLM_PROVIDER=openai`일 때만 필요 |
| `GEMINI_API_KEY` | `laimory-ai/prod/app` | X | `LLM_PROVIDER=gemini`일 때만 필요 |
| `ES_API_KEY` | 로그 전달 Lambda용 별도 Secret | O | CloudWatch 로그를 Elasticsearch로 전송할 때 사용 |

현재 LLM provider는 Bedrock이므로 `OPENAI_API_KEY`와 `GEMINI_API_KEY`는 넣지 않는다.
Bedrock은 AgentCore Runtime 실행 역할로 인증한다.

`APP_SERVER_API_URL`, `BEDROCK_MODEL`, `LANGFUSE_PUBLIC_KEY`, `ES_URL`처럼 비밀이 아닌
값은 Secrets Manager가 아니라 해당 실행 주체의 환경변수에 둔다.
