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
