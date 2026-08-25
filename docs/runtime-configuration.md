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
| `LLM_PROVIDER` | `bedrock` | `bedrock` |
| `PROMPT_VERSION` | `v1` | `v1` |
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

`AGENT_VERSION`은 넣지 않는다. AgentCore에서는 이미지에 설치된 패키지 버전을 사용한다.

로컬에서만 사용하는 API key와 `LANGFUSE_SECRET_KEY`는 gitignored `.env`에 둘 수 있다.
실제 값을 커밋하지 않는다.

## 2. Secrets Manager

production의 `laimory-ai/prod/app` Secret에는 현재 다음 값만 넣는다.

```json
{
  "LANGFUSE_SECRET_KEY": "<운영 프로젝트 secret key>"
}
```

LLM provider를 바꿀 때만 선택한 provider의 key를 추가한다.

- OpenAI 사용 시: `OPENAI_API_KEY`
- Gemini 사용 시: `GEMINI_API_KEY`
- Bedrock 사용 시: 추가 key 없음. AgentCore Runtime 실행 역할로 인증

`APP_SERVER_API_URL`, `BEDROCK_MODEL`, 로그·timeout 설정처럼 비밀이 아닌 값은 Secret에
넣지 않고 AgentCore 환경변수에 둔다.

Elasticsearch의 `ES_API_KEY`는 애플리케이션용 `laimory-ai/prod/app`이 아니라 AgentCore
로그 전달 Lambda가 읽는 별도 Secret에 둔다.
