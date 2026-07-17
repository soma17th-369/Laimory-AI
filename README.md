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
│   ├── observability/         # transaction 기반 관측 계약·sink·마스킹
│   └── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini 등, 확장형)
│
├── api/v1/
│   ├── router.py              # v1 라우터 취합
│   └── timeline.py            # POST /v1/timeline (taskId 접수 → 202), GET /{taskId}
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
    ├── task_store.py          # task 상태 저장
    ├── timeline_runner.py     # 백그라운드 파이프라인
    └── callback.py            # 완료 결과 콜백

tests/
├── agents/                    # Event/Repair Agent 테스트 (live 입력 테스트는 opt-in)
├── api/ · services/ · main/   # 단위 테스트
├── integration/               # 실제 LLM 통합 테스트(opt-in)
└── fixtures/                  # 요청/스냅샷 빌더
```

처리 흐름은 `taskId 접수 → 202 즉시 응답 → DB 조회 → normalize → main agent → 상태 갱신/콜백` 순서입니다.

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

# 사용할 LLM provider (openai | gemini)
LLM_PROVIDER=openai

# OpenAI
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-4o-mini

# Gemini
GEMINI_API_KEY=your-api-key
GEMINI_MODEL=gemini-2.5-flash
```

`.env` 파일은 민감 정보를 포함할 수 있으므로 Git에 올리지 않습니다.

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

---

## Git Ignore

```gitignore
.venv/
.env
.idea/
__pycache__/
*.pyc
```

---

## 실제 LLM 테스트 입력

실제 LLM 테스트 입력은 날짜별 디렉터리로 관리합니다.

```text
data/input/
└── 2026-07-08/
    ├── 2026-07-08.json
    ├── 000_20260708_172720.jpg
    └── ...
```

새 날짜를 추가할 때는 `data/input/<YYYY-MM-DD>/` 디렉터리를 만들고, 같은 날짜의
`<YYYY-MM-DD>.json`과 JSON이 참조하는 사진을 함께 넣습니다. 기본 테스트 날짜는
`2026-07-08`이며 다른 날짜를 실행하려면 다음 환경변수를 지정합니다.

```powershell
$env:LAIMORY_LIVE_DATA_DATE="2026-07-09"
$env:LAIMORY_LIVE_LLM="1"
uv run pytest tests/agents -m live_llm -s
```

Event Agent 5종은 서로 독립된 live 테스트라 필요한 Agent만 파일 단위로 실행할 수
있습니다. 전체 Event → Timeline → Repair 흐름은 별도 통합 테스트로 실행합니다.

```powershell
# 특정 Event Agent만 실행
uv run pytest tests/agents/test_location_event_agent_live_input.py -s

# 전체 파이프라인 실행
uv run pytest tests/integration/test_live_llm_data_fixture.py -s

# 일반 테스트만 실행하고 실제 LLM 호출은 제외
uv run pytest tests -m "not live_llm"
```

실제 LLM 결과는 실행할 때마다 다음처럼 별도 디렉터리에 누적됩니다.

```text
data/output/runs/
└── 2026-07-08/
    └── 20260717T123456.123456+0900-openai-gpt-5/
        ├── metadata.json
        ├── observations.jsonl
        ├── event-agents/
        │   ├── calendar.json
        │   └── ...
        ├── timeline-draft.actual.json
        └── timeline-draft.diff.txt
```

같은 pytest 프로세스에서 실행한 Agent 테스트는 하나의 run 디렉터리를 공유합니다.
여러 프로세스의 결과를 의도적으로 한 실행으로 묶으려면 동일한
`LAIMORY_LIVE_RUN_ID`를 지정할 수 있습니다.

`observations.jsonl`에는 같은 `transactionId`로 연결된 Main/Event/Timeline/Repair/LLM
이벤트와 provider가 보고한 토큰 사용량이 기록됩니다. 기본 서버 관측은 본문을 저장하지
않으며, live JSONL은 민감값을 마스킹한 `SANITIZED` 정책을 사용합니다. 전체 이벤트
계약, 토큰 필드 의미, sink 실패 격리와 테스트 방법은
[타임라인 테스트와 관측 로그](docs/timeline-observability.md)를 참고하세요.

live 테스트는 fallback 초안만으로 성공 처리하지 않습니다. 실제 LLM `RESPONSE`가
없거나 `FAILED` 이벤트가 있으면 provider 연결 실패로 테스트가 실패합니다.
