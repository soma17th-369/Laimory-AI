# 로컬 개발·테스트

## Scope

재현 가능한 로컬 환경, 서버 실행, 설정 범주, 기본·통합·live 테스트의 경계와 현재 CI 상태를 설명한다.

## Read When

- 개발 환경을 처음 구성할 때
- dependency, Python version, Settings, pytest marker를 바꿀 때
- 변경 위험에 맞는 검증 명령을 고를 때

## Authoritative Sources

- `.python-version`, `pyproject.toml`, `uv.lock`
- `app/core/config.py`, `app/server.py`
- `tests/**`, 특히 `tests/core/conftest.py`, `tests/agents/conftest.py`, `tests/integration/**`, live Agent 테스트
- `.github/workflows/**`

## Current Implementation

Python version은 `.python-version`과 `pyproject.toml` 모두 3.14를 요구한다. dependency 관리는 프로젝트 root의 uv와 `.venv`를 사용한다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv sync --locked
uv run uvicorn app.server:app --reload
```

Windows 기본 uv cache 권한 문제가 있을 때만 repository-local `.uv-cache`를 쓴다. 의존성 변경은 `pyproject.toml`과 `uv.lock`을 함께 반영한다. 운영 image는 `uv sync --locked --no-dev`라 pytest 같은 dev dependency를 포함하지 않는다.

Settings는 project root `.env`와 process environment를 읽고 extra key를 무시한다. 서버 import에 최소한 `APP_ENV`, `LOG_LEVEL`, `LLM_PROVIDER`, `APP_SERVER_API_URL`이 필요하다. 선택한 provider를 실제 호출할 때 해당 model과 credential이 추가로 필요하다. secret 값은 `.env`에 둘 수 있지만 Git·Knowledge·로그에 기록하지 않는다.

로컬 Uvicorn 명령은 기본적으로 8000을 쓰고 `/health`, `/docs`로 process와 OpenAPI를 확인할 수 있다. 운영 image는 Docker CMD가 8080으로 고정한다.

### 테스트 층

- `tests/api`: route, error, request logging, lifespan, health adapter
- `tests/core`: Settings, provider, structured output, error, logging·redaction·Langfuse
- `tests/services`: normalizer, source/result 계약, guard/repair, runner/client
- `tests/main`: main Agent graph와 Langfuse graph
- `tests/agents`: Agent parser·prompt·Repair·Photo와 opt-in live input
- `tests/scripts`: deploy/filebeat/prune/smoke script
- `tests/integration`: 실제 LLM과 data fixture를 이용한 opt-in 통합 검증

pytest marker는 `integration`, `live_llm`, `live_es`가 등록돼 있다. 실제 network·비용이 드는 LLM 테스트는 `LAIMORY_LIVE_LLM=1` 없이는 skip한다. provider별 credential/model이 필요하고, strict fixture equality는 별도 opt-in이다. live fixture 테스트는 `data/output`에 actual·diff 파일을 쓸 수 있으므로 일반 unit run과 구분한다.

기본 회귀 실행 예시는 다음과 같다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv run pytest -q -p no:cacheprovider -m "not live_llm"
```

`-p no:cacheprovider`로 `.pytest_cache` 생성을 막는다. `--basetemp`나 repository-local 임시 디렉터리는 사용할 수 있지만, 검증이 끝나면 해당 작업에서 만든 `.pytest-*`, `.test-tmp-*`, `pytest-cache-files-*`를 삭제한다. 삭제 전에는 절대 경로가 repository root 아래이고 해당 임시 이름 패턴에 맞는지 확인한다.

테스트 중 일부 package-level conftest는 기본 `APP_ENV`, `LOG_LEVEL`, `LLM_PROVIDER`를 채우지만 `APP_SERVER_API_URL`까지 공통으로 채우지는 않는다. 환경에 `.env`가 없는 clean runner에서는 명시적인 안전한 test URL 설정이 필요할 수 있다.

## Invariants

- Python과 dependency version은 설정·lock을 따른다.
- 기본 테스트가 실제 LLM, AWS, Langfuse, Elasticsearch 비용을 발생시키지 않게 한다.
- live test는 명시적 opt-in과 필요한 credential 확인 뒤 실행한다.
- 실제 secret·token·사용자 fixture 원문을 테스트 로그나 Knowledge에 복제하지 않는다.
- 변경 범위와 위험에 맞는 대상 테스트를 먼저 실행하고, 공통 경계를 건드리면 전체 non-live suite로 넓힌다.
- test 중 생성한 pytest cache·base temp 디렉터리는 검증 종료 후 작업 트리에 남기지 않는다.

## Known Gaps

- GitHub Actions에 lint·type check·unit test를 실행하는 CI workflow가 없다. `dev` 배포 workflow도 test job을 선행 조건으로 두지 않는다.
- Ruff, mypy, pyright 같은 formatter/linter/type checker 설정이 없다.
- `.env` 없는 clean environment에서 전체 suite가 필요한 최소 환경을 root conftest 한곳에서 정의하지 않는다.
- `tests/scripts/test_filebeat_config.py`는 PyYAML을 `importorskip`하지만 PyYAML이 dev dependency에 명시돼 있지 않아 환경에 따라 skip될 수 있다.

## Update When

Python/uv 절차, 필수 설정, 서버 entrypoint·port, pytest 구조·marker·opt-in 조건, 기본 검증 명령, 테스트 임시물 정리 정책, lint/type/CI 도구가 바뀔 때 갱신한다.

## Validation

- `python --version`, `uv --version`, `uv sync --locked`
- `uv run pytest -q -p no:cacheprovider -m "not live_llm"`
- `uv run python -c "from app.core.config import settings; print(settings.app_env)"`
- `rg -n "markers|requires-python|dependency-groups" pyproject.toml`
- `rg -n "pytest|uv run" .github/workflows tests`
- repository root에 `.pytest-*`, `.test-tmp-*`, `pytest-cache-files-*`가 남지 않았는지 확인
