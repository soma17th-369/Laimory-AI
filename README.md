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
│   ├── config.py              # 설정 (pydantic-settings, LLM_PROVIDER/API 키 등)
│   ├── logging.py             # 관찰 로그 설정
│   ├── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini/Bedrock, 확장형)
│   └── inflight.py            # 진행 중 백그라운드 처리 카운터 (GET /ping 상태 판단용)
│
├── api/
│   ├── agentcore.py           # AgentCore Runtime 계약 (POST /invocations, GET /ping)
│   └── v1/
│       ├── router.py          # v1 라우터 취합
│       └── timeline.py        # POST /v1/timeline (taskId 접수 → 202)
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 입력 계약
│   ├── location.py/calendar.py/health.py/notification.py/photo.py
│   ├── event_candidate.py     # AI 이벤트 후보 모델
│   ├── timeline_request.py    # 정규화된 요청(main agent 입력)
│   ├── repair.py              # Repair Agent 계약(문제 목록 + 도구 호출 계획)
│   └── timeline.py            # 타임라인 초안/이벤트 스키마
│
├── agents/                    # AI 에이전트
│   ├── base.py                # 공통 에이전트 인터페이스
│   ├── parsing.py             # LLM 호출/프롬프트/응답 파싱 유틸
│   ├── events/                # 데이터별 이벤트 에이전트
│   │   └── base_event_agent.py
│   ├── timeline/timeline_agent.py # 후보 → 초안 병합
│   ├── repair/                # 초안 검토·개선 (LLM 분석 + 도구 호출)
│   │   ├── repair_agent.py    # 확정 → 분석 → 도구 실행 → 재확정 반복
│   │   ├── tools.py           # 도구 카탈로그 (서비스·상류 Agent 를 도구로)
│   │   └── prompt.md          # 분석·계획 system prompt
│   └── main/main_agent.py     # events → timeline → repair 조율(LangGraph)
│
└── services/
    ├── source_repository.py   # taskId로 수집 스냅샷 조회
    ├── timeline_repository.py # 최종 timeline_events/timeline_items 저장
    ├── timeline_validator.py  # 최종 저장 전 source 소속·시간 검증
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
    ├── place_resolver.py      # 장소 확정
    ├── place_text.py          # 장소 문자열 정규화·비교
    ├── timeline_runner.py     # 백그라운드 파이프라인
    └── callback.py            # 완료 상태 콜백

tests/
├── agents/                    # Event/Repair Agent 테스트 (live 입력 테스트는 opt-in)
├── api/ · services/ · main/   # 단위 테스트
├── integration/               # 실제 LLM 통합 테스트(opt-in)
└── fixtures/                  # 요청/스냅샷 빌더

Dockerfile                     # EC2/AgentCore 공용 배포 이미지 (non-root, 8080)
.dockerignore                  # 빌드 컨텍스트 허용 목록
.github/workflows/
├── deploy-ec2.yml             # dev push → amd64 이미지 → EC2 자동 배포
├── deploy-agentcore.yml       # arm64 이미지 → AgentCore 수동 복구
└── rollback-agentcore.yml     # AgentCore 수동 롤백
scripts/deploy-ec2.sh          # SSM에서 실행하는 EC2 교체·자동 복구 스크립트
docs/deploy-ec2.md             # EC2 운영 배포 절차
docs/deploy-agentcore.md       # AgentCore 수동 복구 절차
```

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

# 관측(Observability) — 본문을 제외한 Timeline 실행 메타데이터를 ES로 보낸다(#28).
# OBS_ENABLED=false이고 OBS_LOCAL_DIR도 비어 있으면 수집 자체가 no-op이다.
OBS_ENABLED=false            # ES 전송 마스터 스위치
ES_URL=                      # 예: https://es.internal:9200 (비면 전송 안 함)
ES_API_KEY=                  # Elasticsearch ApiKey (선택)
AGENT_VERSION=               # 선택: 이미지 tag/commit SHA. 기본은 패키지 버전
OBS_MAX_PAYLOAD_BYTES=16384  # 이벤트별 payload 최대 byte
OBS_MAX_EVENTS_PER_TASK=1000 # task별 메모리 버퍼 이벤트 상한
OBS_LOCAL_DIR=               # dev 검사용 events.jsonl 저장 경로
ES_EVENT_INDEX=ai-timeline-task # 단계별 실행 이벤트 인덱스 base
LOG_FORMAT=rich              # 운영은 json (stdout JSON → CloudWatch Logs Insights)
```

`.env` 파일은 민감 정보를 포함할 수 있으므로 Git에 올리지 않습니다.

---

## LLM Provider 전환과 토큰 로그

`LLM_PROVIDER` 한 줄로 `openai` / `gemini` / `bedrock` 을 전환합니다. 각 provider 는
자신의 `{PROVIDER}_MODEL` 을 사용합니다(openai=GPT, gemini=Gemini, bedrock=Nova).

- **openai / gemini**: 키와 모델을 `.env` 에 넣으면 됩니다.
- **bedrock**: 로컬에서는 `.env`에 `BEDROCK_AWS_PROFILE`을 지정하고 실제 Access Key와
  Secret은 `aws configure --profile <이름>`으로 `~/.aws/credentials`에 보관합니다.
  배포 환경(`APP_ENV=prod`)에서는 이 값이 있더라도 무시하고 EC2 Instance Role 또는
  AgentCore Runtime 실행 역할을 씁니다.

타임라인을 어떤 provider/모델로 만들었는지와 각 LLM 호출의 토큰 사용량은 **로그**로
남습니다. 비용은 AWS Cost Explorer에서 확인하며, 호출별 토큰 양은 아래 로그로 확인합니다.

```text
메인 에이전트 시작: taskId=..., agents=N, provider=bedrock, model=global.amazon.nova-2-lite-v1:0
LLM 토큰 사용량: provider=bedrock, model=..., inputTokens=123, outputTokens=45
```

`aws configure --profile laimory-bedrock`으로 로컬 자격증명을 설정하려면 AWS CLI가
필요합니다. 배포 환경에서는 프로필 설정을 제거하면 AWS 실행 역할로 동작합니다.

---

## 관측 (Observability)

Timeline 요청(`taskId`) 하나의 Agent·LLM·저장·콜백 실행 메타데이터를 구조화 로그로 모아
**Elasticsearch → Kibana** 에서 `taskId` 로 조회한다. 입력·사용자 메모리·프롬프트·LLM 응답·
draft·도구 인자/결과 본문은 저장하지 않고 필요한 경우 길이와 해시만 남긴다. 운영 로그(FastAPI 등)는
`LOG_FORMAT=json` 으로 stdout JSON 을 남겨 **CloudWatch** 가 수집한다. 관측 전송은
실패해도 Timeline 처리에 영향을 주지 않는다.

- 전체 설계·이벤트 계약·Kibana 조회·설정: [docs/timeline-observability.md](docs/timeline-observability.md)
- ES 인덱스 매핑: [ai-timeline-task](docs/observability/ai-timeline-task-index-template.json)

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
