# Laimory-AI 에이전트 지침

이 저장소는 FastAPI 기반 Python 서버 프로젝트입니다. Codex와 Claude가 같은 프로젝트 지침을 공유할 수 있도록 이 파일을 공통 기준으로 사용합니다.

## 기본 작업 방식
- 모든 md 파일은 한글을 base 로 생성합니다.
- 변경 전에는 관련 파일을 먼저 읽고 현재 구조를 기준으로 판단합니다.
- 불필요한 리팩터링이나 unrelated 변경은 하지 않습니다.
- 사용자가 명시하지 않은 파일 삭제, git reset, checkout 같은 파괴적 작업은 하지 않습니다.
- 기존 변경사항이 있으면 사용자 작업으로 보고 되돌리지 않습니다.

## Python 환경

- Python 버전은 `.python-version`과 `pyproject.toml` 기준을 따릅니다.
- 이 프로젝트는 `uv`와 `.venv`를 사용합니다.
- 의존성 설치는 프로젝트 루트에서 `uv sync`를 사용합니다.
- Windows에서 기본 uv 캐시 권한 문제가 있으면 다음처럼 로컬 캐시를 사용합니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv sync
```

## 실행

FastAPI 앱 진입점은 `app.server:app`입니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv run uvicorn app.server:app --reload
```

## 스킬 공유

- Codex용 프로젝트 스킬 원본은 `.agents/skills/` 아래에 둡니다.
- Claude 쪽에서 공유할 때는 `.agents/skills/` 내용을 `.claude/skills/`로 복사해 동기화합니다.
- `.claude/skills/`는 링크가 아니라 복사본이며, 필요할 때 `scripts/link-skills.ps1` 또는 `scripts/link-skills.sh`를 다시 실행해 갱신합니다.

## Project Structure
```
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