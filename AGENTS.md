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
- Claude 쪽에서 공유할 때는 `.claude/skills/`가 같은 원본을 바라보도록 구성합니다.
- 경로 문자열만 들어 있는 파일은 실제 디렉터리 링크가 아닐 수 있으니, 링크 여부는 `LinkType` 또는 `Attributes`로 확인합니다.
