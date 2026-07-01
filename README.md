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
│   ├── logging.py             # 관찰 로그 설정            ← 체크7
│   └── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini 등, 확장형)
│
├── api/v1/
│   ├── router.py              # v1 라우터 취합
│   └── timeline.py            # POST /v1/timeline (초안 생성 엔드포인트)
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── source_item.py         # 소스 아이템 명세          ← 체크1
│   ├── event_candidate.py     # AI 이벤트 후보 모델       ← 체크2
│   └── timeline.py            # 타임라인 초안/이벤트 스키마
│
├── agents/                    # AI 에이전트
│   ├── base.py                # 공통 에이전트 인터페이스
│   ├── prompts/               # 프롬프트 템플릿 분리
│   ├── events/                # 데이터별 이벤트 에이전트   ← 체크3
│   │   ├── base_event_agent.py
│   │   └── location_agent.py  (등 소스별로 추가)
│   └── timeline_agent.py      # 후보 → 초안 병합          ← 체크4
│
├── pipeline/
│   └── timeline_pipeline.py   # normalize→events→timeline→validate 조율 ← 체크5
│
└── services/
    ├── normalizer.py          # 입력 → 공통 이벤트 후보 정규화
    ├── validator.py           # AI 결과 검증               ← 체크6
    └── storage.py             # 검증 통과분 최종 저장       ← 체크6

tests/
├── agents/
├── pipeline/
└── fixtures/                  # MVP 테스트 케이스          ← 체크7
```

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

의존성은 `pyproject.toml`의 `dependencies`에 직접 추가합니다.

예시:

```toml
[project]
name = "laimory-ai"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "fastapi>=0.136.3",
    "uvicorn[standard]>=0.49.0",
    "python-dotenv>=1.1.0",
    "rich>=14.0.0",
]
```

의존성을 추가하거나 제거한 뒤 PyCharm 실행 버튼을 누르면, 실행 전에 `uv sync`가 자동으로 실행되어 변경 사항이 반영됩니다.

즉, 실행 흐름은 다음과 같습니다.

```text
pyproject.toml 수정
→ PyCharm Run 버튼 클릭
→ uv sync 실행
→ FastAPI 서버 실행
```

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

`python -m app.server`처럼 모듈을 직접 실행하는 경우에는 `.env`의
`SERVER_HOST`, `SERVER_PORT` 값이 사용됩니다.

실행 후 아래 주소에서 확인할 수 있습니다.

```text
http://localhost:8000/health
http://localhost:8000/docs
```

---

## PyCharm One-Click Run Configuration

PyCharm에서 실행 버튼 한 번으로 `uv sync` 후 FastAPI 서버가 실행되도록 설정합니다.

### 1. Python Interpreter 설정

PyCharm에서 프로젝트 인터프리터를 `.venv`로 설정합니다.

```text
Settings
→ Python Interpreter
→ Add Interpreter
→ Existing Environment
```

인터프리터 경로:

```text
.venv/Scripts/python.exe
```

Mac/Linux의 경우:

```text
.venv/bin/python
```

---

### 2. FastAPI 실행 설정

PyCharm 상단 오른쪽의 실행 설정 드롭다운을 클릭합니다.

```text
Add Configuration...
또는
Edit Configurations...
```

다음 순서로 설정을 추가합니다.

```text
+ → Python
```

설정값:

```text
Name:
FastAPI

Script path:
.venv/Scripts/uvicorn.exe

Parameters:
app.server:app --reload

Working directory:
$ProjectFileDir$
```

Mac/Linux의 경우 Script path:

```text
.venv/bin/uvicorn
```

---

### 3. 실행 전 uv sync 자동 실행 설정

FastAPI Run Configuration 화면 하단의 `Before launch` 영역에서 설정합니다.

```text
Before launch
→ +
→ Run External Tool
→ +
```

External Tool 값을 다음과 같이 입력합니다.

```text
Name:
uv sync

Program:
uv

Arguments:
sync

Working directory:
$ProjectFileDir$
```

만약 `uv` 명령어가 인식되지 않는다면, `Program`에는 uv 실행 파일의 절대경로를 입력합니다.

예시:

```text
<uv-install-dir>/uv.exe
```

설정 후 다음 순서로 저장합니다.

```text
OK → Apply → OK
```

이제 PyCharm의 초록색 실행 버튼을 누르면 다음 순서로 실행됩니다.

```text
1. uv sync
2. uvicorn app.server:app --reload
```

---

## Health Check

서버 실행 후 아래 API로 동작을 확인합니다.

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

`.gitignore`에 다음 항목을 추가합니다.

```gitignore
.venv/
.env
.idea/
__pycache__/
*.pyc
```
