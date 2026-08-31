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
| `BEDROCK_MAX_TOKENS` | 생략하면 `16384` | 생략하면 `16384` |
| `LLM_MODEL_FAST` | 생략 가능. 넣으면 FAST 티어 모델 ID | 생략 가능. 넣으면 FAST 티어 모델 ID |
| `LLM_MODEL_QUALITY` | 생략 가능. 넣으면 QUALITY 티어 모델 ID | 생략 가능. 넣으면 QUALITY 티어 모델 ID |
| `APP_SERVER_API_URL` | 로컬 App Server URL | 같은 VPC의 App Server 내부 URL |
| `LANGFUSE_ENABLED` | 필요에 따라 `true`/`false` | `true` |
| `LANGFUSE_PUBLIC_KEY` | 로컬 프로젝트 public key | 운영 프로젝트 public key |
| `LANGFUSE_BASE_URL` | `https://jp.cloud.langfuse.com` | `https://jp.cloud.langfuse.com` |
| `LANGFUSE_CONTENT_CAPTURE` | 생략하면 `SANITIZED` | `NONE` |
| `TIMELINE_TEST_ENABLED` | 생략하면 `true`(`APP_ENV=local`) | **넣지 않음.** 넣으면 운영에 테스트 경로가 열림 |
| `SECRETS_BUNDLE_NAME` | 기본적으로 넣지 않음 | `laimory-ai/prod/app` |

AgentCore가 이 값을 컨테이너 프로세스 환경으로 주입하므로 애플리케이션은 `.env`와 동일하게
`pydantic-settings`로 읽는다. AgentCore 환경변수를 boto3로 다시 조회하지 않는다.

`SERVER_HOST`와 `SERVER_PORT`는 `python -m app.server`로 실행할 때만 사용한다. Docker와
AgentCore는 `uvicorn --host 0.0.0.0 --port 8080`으로 실행하므로 두 환경변수를 사용하지 않는다.

`AGENT_VERSION`은 넣지 않는다. AgentCore에서는 이미지에 설치된 패키지 버전을 사용한다.

`TIMELINE_TEST_ENABLED`는 동기 테스트 엔드포인트(`POST /v1/timeline/test`, #102)의 개폐를
정한다. 값을 지정하면 `APP_ENV`보다 우선하고, 생략하면 `APP_ENV`가 `local`/`dev`일 때만
열린다. production에는 **넣지 않는다** — 넣어서 `true`로 두면 운영 컨테이너에 테스트
경로가 열린다.

**EC2(개발) 컨테이너에서는 이 값을 명시해야 한다.** 그 인스턴스의
`/opt/laimory-ai/runtime.env`는 `APP_ENV=prod`로 운영되므로(같은 이유로 #48에서
Langfuse 본문이 보이지 않았다) 생략하면 dev 서버에서도 닫힌다. 절차는
[EC2 배포 가이드](deploy-ec2.md#동기-테스트-엔드포인트-102)를 따른다.

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

## 모델 교체 (#98)

Bedrock provider 는 모델별 분기를 갖지 않는다. `BEDROCK_MODEL` 하나로 바꾼다.

| 모델 | `BEDROCK_MODEL` |
|---|---|
| Amazon Nova 2 Lite | `global.amazon.nova-2-lite-v1:0` |
| OpenAI GPT-5.6 Luna | `global.openai.gpt-5.6-luna` |

서울(`ap-northeast-2`)에는 두 모델 모두 **Global cross-Region inference 로만** 있다.
`apac.` geo 프로필과 in-Region 호출은 없으므로 `global.` 접두를 뗀 id 를 쓰지 않는다.

`BEDROCK_MAX_TOKENS` 를 비우지 않는다. Converse `inferenceConfig.maxTokens` 를 주지
않으면 모델이 tool call 을 만들다 형식을 깨뜨려 `stopReason=malformed_tool_use` 와 빈
content 를 담은 **HTTP 200** 이 돌아온다(#98). 기본값 `16384` 는 하루치 입력에서 관측된
최대 출력(8,705 토큰)을 담을 수 있는 값이다.

`APP_SERVER_API_URL`, `BEDROCK_MODEL`, `LANGFUSE_PUBLIC_KEY`, `ES_URL`처럼 비밀이 아닌
값은 Secrets Manager가 아니라 해당 실행 주체의 환경변수에 둔다.

## 단계별 모델 티어 (#106)

provider 는 `LLM_PROVIDER` 하나로 전역이고 **모델만** 두 티어로 갈린다. 어느 단계가 어느
티어인지는 `app/core/llm_stages.py` 의 매핑표가 소유한다.

| 티어 | 환경변수 | 단계 |
|---|---|---|
| FAST | `LLM_MODEL_FAST` | location, calendar, photo, photo_describe, sleep_activity, notification |
| QUALITY | `LLM_MODEL_QUALITY` | timeline, repair, question, user_memory |

**두 변수 모두 선택이다.** 비워 두면 그 티어는 전역 `{PROVIDER}_MODEL`(현재
`BEDROCK_MODEL`)을 쓴다. 둘 다 넣지 않으면 동작이 지금과 같으므로 기존 `.env`·EC2
`runtime.env`·AgentCore 환경변수를 바꾸지 않아도 된다.

티어 이름은 **모델 자체의 성질**만 가리킨다. 단계 배치는 운영하며 바꾸는 값이라 이름에
용도를 담으면 배치를 바꾸는 순간 이름이 거짓이 된다.

주의할 점 세 가지다.

- **`LLM_MODEL_FAST` 에는 vision 을 지원하는 모델을 넣는다.** `photo_describe` 가 이미지
  입력을 쓰는 유일한 단계라, 지원하지 않는 모델을 넣으면 사진 설명이 통째로 실패한다.
  Photo Event Agent 가 실패를 warning 으로 흡수하므로 타임라인은 나오지만 사진 근거가 빈다.
- **모델을 바꾼 직후 첫 실행은 prompt cache 가 콜드라 latency 가 튄다.** 이것을 모델 성능
  차이로 읽지 않는다. (Bedrock 은 Converse 에 `cachePoint` 를 넣어야 캐시가 걸리는데 현재
  코드에는 없으므로, 지금은 provider 자동 캐싱이 없는 상태다.)
- 잘못된 모델 ID 를 넣으면 그 티어의 단계만 `LLM_CALL_FAILED(1203)` 로 실패한다. 설정만
  비우면 즉시 전역 모델로 돌아간다.

실제 호출에 쓰인 모델은 Langfuse generation 의 `model` 에 호출마다 남는다. 단계는
generation 이름(`infer-location-events`, `generate-timeline-draft` 등)이 가른다. Main Agent
운영 로그에는 전역 기본값 `model` 과, 전역과 다를 때만 `modelFast`/`modelQuality` 가 붙는다.
