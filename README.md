# Laimory-AI

Laimory AI 서버 프로젝트입니다.

FastAPI 기반으로 구성되어 있으며 Python 의존성 관리는 `uv`와 `pyproject.toml`을 사용합니다.

---

## Tech Stack

- Python 3.14
- FastAPI
- Uvicorn
- uv
- PyCharm

---

## Project Structure

```text
app/
├── server.py                  # FastAPI 앱 생성 + 라우터 등록만 (얇게)
│
├── core/                      # 공통 인프라
│   ├── config.py              # 설정 (LLM_PROVIDER, PROMPT_VERSION, API 키 등)
│   ├── logging.py             # 로그 출력 설정 (rich | 한 줄 JSON)
│   ├── operational_logging.py # Elasticsearch 로 나가는 운영 이벤트의 유일한 통로 (#53).
│   │                          #   event.dataset 표식 + 이벤트별 필드 allowlist
│   ├── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini/Bedrock, 확장형)
│   ├── inflight.py            # 진행 중 백그라운드 처리 카운터 (GET /ping 상태 판단용)
│   ├── secret_bundle.py       # 시크릿 번들 로딩 + pydantic-settings 소스 (#30)
│   └── secrets.py             # 시크릿 조회 진입점 (#30). 번들 > 환경변수/.env > 기본값
│
├── api/
│   ├── agentcore.py           # AgentCore Runtime 계약 (POST /invocations, GET /ping).
│   │                          #   requestType(TIMELINE/USER_MEMORY_UPDATE)으로 기존 핸들러에 위임
│   └── v1/
│       ├── router.py          # v1 라우터 취합
│       └── timeline.py        # POST /v1/timeline (taskId+taskToken 접수 → 202)
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 파이프라인 내부 계약
│   ├── timeline_input.py      # App Server 입력 조회 응답 계약
│   ├── timeline_result.py     # App Server 결과 저장 요청 계약
│   ├── location.py/calendar.py/health.py/notification.py/photo.py
│   ├── event_candidate.py     # AI 이벤트 후보 모델
│   ├── timeline_request.py    # 정규화된 요청(main agent 입력)
│   ├── repair.py              # Repair Agent 계약(문제 목록 + 도구 호출 계획)
│   └── timeline.py            # 타임라인 초안/이벤트 스키마
│
├── agents/                    # AI 에이전트
│   ├── base.py                # 공통 에이전트 인터페이스
│   ├── parsing.py             # LLM 호출/프롬프트/응답 파싱 유틸
│   ├── prompt_loader.py       # PROMPT_VERSION에 맞는 Agent 프롬프트 로드
│   ├── events/                # 데이터별 이벤트 에이전트
│   │   └── base_event_agent.py
│   ├── timeline/timeline_agent.py # 후보 → 초안 병합
│   ├── repair/                # 초안 검토·개선 (LLM 분석 + 도구 호출)
│   │   ├── repair_agent.py    # 확정 → 분석 → 도구 실행 → 재확정 반복
│   │   ├── tools.py           # 도구 카탈로그 (서비스·상류 Agent 를 도구로)
│   │   └── prompts/v1/prompt.md # 분석·계획 system prompt v1
│   └── main/main_agent.py     # events → timeline → repair 조율(LangGraph)
│
└── services/
    ├── app_server_client.py   # App Server API 클라이언트 (입력 조회/결과 저장/콜백 + taskToken)
    ├── source_contract.py     # 입력 조회 응답 묶음 계약 검증
    ├── timeline_result.py     # draft → 결과 저장 요청 변환
    ├── timeline_validator.py  # 결과 저장 전 source 소속·시간 검증
    ├── normalizer.py          # 수집 스냅샷 분리·정규화
    ├── draft_repair.py        # draft 확정 repair
    ├── draft_edit.py          # event 수정·삭제 (Repair 계획의 결정론 적용)
    ├── validator.py           # 요청 시간 범위(window) 강제
    ├── source_lookup.py       # sourceRef → 입력 항목 역참조, sourceType 정정
    ├── sleep_guard.py         # 수면 경계 강제 (기상 이전 event 제거)
    ├── stay_merge.py          # 이동 없이 이어진 STAY 묶기
    ├── calendar_guard.py      # 누락된 캘린더 일정 복원
    ├── calendar_location.py   # 캘린더와 STAY 장소 일치 보강
    ├── meal_guard.py          # MEAL 지속시간 강제
    ├── photo_guard.py         # 사진 단일 귀속 검사 (누락·중복 검출, 자동 해소는 안 함)
    ├── location_metrics.py    # Location raw 파생 지표 계산 (속도·구간 공백·수집 공백)
    ├── location_guard.py      # Location 결과 검증 (상위 여정 누락·공백 표시·rawId 보존)
    ├── fragment_guard.py      # candidate·fragment 보존 검사
    ├── notification_guard.py  # 알림·최종 draft의 민감정보·근거 없는 관계명 검사
    ├── place_resolver.py      # 장소 확정
    ├── place_text.py          # 장소 문자열 정규화·비교
    └── timeline_runner.py     # 백그라운드 파이프라인 (입력 조회→추론→결과 저장→콜백)

tests/
├── agents/                    # Event/Repair Agent 테스트 (live 입력 테스트는 opt-in)
├── api/ · services/ · main/   # 단위 테스트
├── integration/               # 실제 LLM 통합 테스트(opt-in)
└── fixtures/                  # 요청/스냅샷 빌더

Dockerfile                     # EC2/AgentCore 공용 배포 이미지 (non-root, 8080)
.dockerignore                  # 빌드 컨텍스트 허용 목록
.github/workflows/
├── deploy-ec2.yml             # dev push → amd64 이미지 → EC2 자동 배포(개발)
├── deploy-production.yml      # main push → 승인 → arm64 이미지 → AgentCore 배포(운영)
├── rollback-production.yml    # 엔드포인트를 이전 Runtime 버전으로 되돌림(재빌드 없음)
└── pr-main-guard.yml          # main 대상 PR 이 dev 에서 왔는지 검사(필수 check)
scripts/deploy-ec2.sh          # SSM에서 실행하는 EC2 교체·자동 복구 스크립트
docs/deploy-production.md      # main→production 승격·배포·롤백과 GitHub·AWS 설정(#90)
docs/deploy-ec2.md             # EC2(개발) 배포 절차
docs/deploy-agentcore.md       # AgentCore 컨테이너 계약과 AWS 자원 준비
docs/agentcore-cutover-manual.md # AgentCore 전환 수동 작업 매뉴얼(#89)
```

배포는 브랜치로 갈립니다. `dev` push 는 EC2(개발, amd64), `main` push 는 AgentCore
Runtime(운영, arm64)으로 나가며 ECR 저장소와 IAM 역할이 분리돼 있습니다(#90).

처리 흐름은 `taskId 접수 → 202 즉시 응답 → DB 조회 → normalize → main agent → timeline_events/timeline_items 저장 → 완료 상태 콜백` 순서입니다.

Main Agent 그래프는 `run_event_agents → merge_results → run_timeline_agent → run_repair_agent` 순서로 실행됩니다.

Repair Agent 는 Timeline Agent 가 만든 초안을 검토·개선해 최종 초안을 확정합니다. 한 번의 실행은
`repair_draft(코드 확정) → 분석(LLM) → 도구 실행 → repair_draft(재확정)` 을 되풀이하며, 반복은
LLM 이 `done` 을 내거나 `settings.repair_max_iterations`(기본 3)에서 멈춥니다. LLM 호출이나 응답
파싱이 실패하면 마지막으로 확정된 초안을 그대로 돌려주고 warning 을 남깁니다.

Repair Agent 는 초안을 직접 다시 쓰지 않고 **결정론 서비스와 상류 Agent 를 도구로 호출**합니다
(`lookup_source`, `update_event`/`delete_event`, `enforce_sleep_boundary` 같은 서비스 재적용,
`rerun_event_agent`/`rerun_timeline_agent`). 정렬·`clientEventId` 재부여·window 강제는 도구가
아니며, 매 반복 끝의 `repair_draft` 가 항상 코드로 확정합니다.

---

## Prerequisites

Python 3.14 이상이 필요합니다.

```bash
python --version
```

uv 설치 여부를 확인합니다.

```bash
uv --version
```

설치되어 있지 않다면 아래 명령어로 설치합니다.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

---

## Install Dependencies

프로젝트 루트에서 실행합니다.

```bash
uv sync
```

`pyproject.toml`과 `uv.lock`을 기준으로 의존성이 설치됩니다.

---

## Dependency Management

의존성은 `pyproject.toml`의 `dependencies`에 추가합니다. 의존성을 변경한 뒤 `uv sync`를 실행해 반영합니다.

---

## Environment Variables

프로젝트 루트에 `.env` 파일을 생성합니다.

```env
APP_ENV=local
LOG_LEVEL=INFO
SERVER_HOST=127.0.0.1
SERVER_PORT=8000

# 사용할 LLM provider (openai | gemini | bedrock)
LLM_PROVIDER=openai

# 모든 Agent가 사용할 프롬프트 세트
PROMPT_VERSION=v1

# OpenAI
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-4o-mini

# Gemini
GEMINI_API_KEY=your-api-key
GEMINI_MODEL=gemini-2.5-flash

# Bedrock (Amazon Nova)
# 로컬 프로필 이름만 .env 에 두고 실제 키는 ~/.aws/credentials 에 저장한다.
# 배포 환경(APP_ENV=prod)에서는 이 값을 무시하고 AWS 실행 역할을 자동 사용한다.
BEDROCK_AWS_PROFILE=laimory-bedrock
# BEDROCK_MODEL 은 Nova 모델 id 또는 크로스리전 추론 프로필 id.
# Nova 2 Lite 는 서울에서 Global inference profile(global. 접두)로 호출한다.
# 서울 리전 IAM 사용자로 실제 converse 호출까지 확인했다(2026-07-23).
BEDROCK_REGION=ap-northeast-2
BEDROCK_MODEL=global.amazon.nova-2-lite-v1:0

# 운영 로그 — 로컬은 rich, 운영은 한 줄 JSON(stdout → Filebeat → Elasticsearch, #47).
LOG_FORMAT=rich              # rich(로컬 콘솔) | json(운영). 운영에서는 반드시 json
AGENT_VERSION=               # 선택: 이미지 tag/commit SHA. 기본은 패키지 버전

# Langfuse tracing — Agent 계층, LLM generation, token/cost 분석
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=         # Langfuse project public key
LANGFUSE_SECRET_KEY=         # Langfuse project secret key. Git에 커밋하지 않는다.
LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
LANGFUSE_SAMPLE_RATE=1.0
# 비워 두면 APP_ENV로 정한다(local/dev=SANITIZED, 그 외=NONE). 적으면 그 값이 이긴다.
# NONE 에서도 durationMs/tokenUsage/errorCode 같은 진단 지표는 남는다.
LANGFUSE_CONTENT_CAPTURE=     # NONE(진단 지표+길이/해시) | SANITIZED(마스킹 본문)
LANGFUSE_MAX_PAYLOAD_BYTES=65536

LOG_FORMAT=rich              # 운영은 json (stdout JSON → CloudWatch Logs Insights)
```

`.env` 파일은 민감 정보를 포함할 수 있으므로 Git에 올리지 않습니다.

`PROMPT_VERSION`은 Event/Timeline/Repair Agent가 사용할 프롬프트 세트를 한 번에
선택합니다. 현재 `v1`, `v2`를 지원하며 기본값은 `v1`입니다. 각 Agent의 프롬프트는
`prompts/{version}/` 아래에 있습니다.
지원하지 않는 버전이나 파일이 빠진 버전은 `v1`로 자동 대체하지 않고 기동 단계에서
오류로 처리합니다. 새 버전을 추가할 때는 모든 Agent의 프롬프트 세트를 준비한 뒤
`Settings.prompt_version`의 지원 목록을 함께 확장합니다.

---

## LLM Provider 전환과 토큰 로그

`LLM_PROVIDER` 한 줄로 `openai` / `gemini` / `bedrock` 을 전환합니다. 각 provider 는
자신의 `{PROVIDER}_MODEL` 을 사용합니다(openai=GPT, gemini=Gemini, bedrock=Nova).

- **openai / gemini**: 키와 모델을 `.env` 에 넣으면 됩니다.
- **bedrock**: 로컬에서는 `.env`에 `BEDROCK_AWS_PROFILE`을 지정하고 실제 Access Key와
  Secret은 `aws configure --profile <이름>`으로 `~/.aws/credentials`에 보관합니다.
  배포 환경(`APP_ENV=prod`)에서는 이 값이 있더라도 무시하고 EC2 Instance Role 또는
  AgentCore Runtime 실행 역할을 씁니다.

타임라인을 어떤 provider/모델로 만들었는지와 각 LLM 호출의 토큰 사용량은 **Langfuse**로
남습니다(이슈 #53). 비용은 AWS Cost Explorer에서, 호출별 토큰 양은 Langfuse의
generation usage에서 확인합니다. Langfuse를 끈 로컬 환경에서는 같은 값이 컨테이너
로그에 DEBUG로 남습니다.

```text
LLM 토큰 사용량: provider=bedrock, model=..., inputTokens=123, outputTokens=45
```

`aws configure --profile laimory-bedrock`으로 로컬 자격증명을 설정하려면 AWS CLI가
필요합니다. 배포 환경에서는 프로필 설정을 제거하면 AWS 실행 역할로 동작합니다.

---

## 관측 (Observability)

관측은 두 갈래이고 서로 대체하지 않는다(이슈 #47, #53).

- **Langfuse** — AI agent 실행 관측. agent 트리, LLM generation, 프롬프트·응답 본문,
  단계별 지연, token usage. 본문은 `LANGFUSE_CONTENT_CAPTURE` 정책으로 마스킹한 뒤 내보낸다.
- **Elasticsearch** — FastAPI **운영 이벤트**. HTTP 요청 완료, 서버 시작·종료,
  백그라운드 작업 완료, 외부 API 연동 결과·재시도. 앱은 stdout에 한 줄 JSON만 쓰고,
  EC2에서 별도 Filebeat 컨테이너가 `logs-laimory.ai-<env>` data stream으로 실어 나른다.

**수집 대상은 코드가 명시한 운영 이벤트뿐이다(#53).**
[`app/core/operational_logging.py`](app/core/operational_logging.py)의 emitter만
`event.dataset=laimory.api` 표식을 만들 수 있고, 이벤트마다 허용 필드가 정해져 있다.
표식이 없는 줄(일반 앱 로그, Agent/LLM 진단, 서드파티 로그, 비JSON 출력)은 Filebeat가
`drop_event`로 버린다. 그래서 로그 호출을 새로 추가해도 수집 범위는 넓어지지 않는다.

**애플리케이션은 Elasticsearch를 직접 호출하지 않는다.** ES URL도 자격증명도 앱 설정에
없으며, 그 경계는 정적 검색 테스트가 지킨다. 프롬프트·LLM 응답·draft 전문·사용자 원문·
예외 원문·traceback은 운영 이벤트에 남지 않는다.

`event.action`으로 이벤트 종류를, `taskId`로 한 실행을, `errorCode`로 실패를 필터·집계한다.
`POST /v1/timeline`의 202 응답시간과 이후 백그라운드 전체 처리시간은 서로 다른 이벤트
(`http.request.completed` / `timeline.task.completed`)로 확인한다.

- 로그 필드 계약·조회 예시·smoke test: [docs/operational-logging.md](docs/operational-logging.md)
- Langfuse trace 구조·보안·설정·검증: [docs/langfuse-tracing.md](docs/langfuse-tracing.md)
- 오류 코드 카탈로그: [docs/error-codes.md](docs/error-codes.md)
- Filebeat 설정 템플릿: [filebeat.example.yml](docs/observability/filebeat.example.yml)

---

## Run Server

터미널에서 직접 실행할 경우:

```bash
uv run uvicorn app.server:app --reload
```

실행 후 아래 주소에서 확인할 수 있습니다.

```text
http://localhost:8000/health
http://localhost:8000/docs
```

---

## PyCharm One-Click Run Configuration

프로젝트 인터프리터를 `.venv/Scripts/python.exe`로 설정하고 다음 실행 구성을 사용합니다.

```text
Name: FastAPI
Script path: .venv/Scripts/uvicorn.exe
Parameters: app.server:app --reload
Working directory: $ProjectFileDir$
```

필요하면 `Before launch`에 `uv sync` External Tool을 추가합니다.

---

## Health Check

```text
GET /health
```

예상 응답:

```json
{
  "status": "ok"
}
```

AgentCore Runtime 배포 환경에서는 `GET /ping`을 함께 제공합니다.

```json
{ "status": "Healthy" }
```

접수한 작업을 아직 처리하고 있으면 `HealthyBusy`를 반환해 컨테이너가 회수되지 않도록 합니다.

---

## Deploy

기본 운영 경로는 EC2 단일 컨테이너입니다. `dev` 브랜치에 push하면 GitHub Actions가
`linux/amd64` 이미지를 Amazon ECR에 올리고, Systems Manager로 EC2 컨테이너를
교체합니다. AgentCore Runtime 배포는 장애가 해소됐을 때 사용할 수동 복구 경로로
유지합니다.

- EC2 자동 배포와 초기 준비: [docs/deploy-ec2.md](docs/deploy-ec2.md)
- AgentCore 전환 시 사람이 AWS·GitHub에서 해야 하는 작업: [docs/agentcore-cutover-manual.md](docs/agentcore-cutover-manual.md)
- AgentCore 수동 배포와 롤백: [docs/deploy-agentcore.md](docs/deploy-agentcore.md)

로컬에서 이미지를 확인하려면:

```bash
docker build -t laimory-ai:local .
docker run --rm -p 8080:8080 laimory-ai:local
curl http://127.0.0.1:8080/ping
```

---

## Git Ignore

```gitignore
.venv/
.env
.idea/
__pycache__/
*.pyc
```
