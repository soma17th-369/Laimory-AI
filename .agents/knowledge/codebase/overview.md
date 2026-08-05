# 프로젝트 개요

## Scope

Laimory-AI의 기술 스택, 실행 단위, 패키지 책임과 연결된 외부 시스템을 빠르게 파악하기 위한 문서다. 개별 API 필드나 클래스 목록은 다루지 않는다.

## Read When

- 처음 저장소를 탐색하거나 구현 위치를 정할 때
- 핵심 dependency, 최상위 package, 외부 시스템 경계를 바꿀 때
- README와 실제 코드가 일치하는지 판단할 때

## Authoritative Sources

- `.python-version`, `pyproject.toml`, `uv.lock`
- `app/server.py`, `app/api/**`, `app/core/**`, `app/agents/**`, `app/services/**`, `app/schemas/**`
- `Dockerfile`, `.dockerignore`, `tests/**`

## Current Implementation

이 프로젝트는 Python 3.14와 FastAPI/Uvicorn으로 실행되는 무상태 AI 서버다. 의존성은 uv가 `pyproject.toml`과 `uv.lock`으로 관리한다. Pydantic Settings가 환경 설정을, Pydantic 모델이 inbound·outbound·내부 데이터 계약을 맡는다.

핵심 런타임은 LangGraph로 조율하는 다단계 Timeline 생성이다. 데이터 종류별 Event Agent가 후보를 만들고 Timeline Agent가 합친 뒤, Repair Agent가 결정론 서비스와 제한된 LLM 도구 실행을 반복한다. 최종 event별 회고 질문을 생성하는 Question Agent는 Repair 뒤에 연결돼 있다.

| 패키지 | 책임 |
|---|---|
| `app/api` | FastAPI inbound route, 공통 오류 응답, 요청 완료 운영 이벤트 |
| `app/core` | 설정, LLM provider, structured output, 오류 코드, 실행 컨텍스트, 로깅·관측·마스킹 |
| `app/schemas` | 전송 계약과 파이프라인 내부 계약 |
| `app/agents` | Event/Timeline/Repair/Question Agent와 prompt 로딩 |
| `app/services` | App Server 연동, 정규화, 결정론 검증·보정, 결과 변환, task orchestration |
| `tests` | API·계약·guard·관측·배포 스크립트 회귀 방어와 opt-in live 검증 |

외부 시스템은 App Server API, 선택한 LLM provider(OpenAI, Gemini, Amazon Bedrock), 선택적 Langfuse, ECR·EC2·SSM 또는 AgentCore Runtime이다. Elasticsearch는 애플리케이션 외부의 Filebeat가 stdout 운영 이벤트를 전달하며 앱이 직접 호출하지 않는다.

운영 컨테이너는 8080에서 한 개 Uvicorn worker로 실행된다. 로컬 직접 실행 기본 포트는 Settings의 8000이며 명령행 Uvicorn 옵션으로 바꿀 수 있다.

## Invariants

- 제품 데이터와 task 상태의 소유자는 App Server다.
- AI 서버의 제품 데이터 접근은 App Server HTTP API를 통해서만 이뤄진다.
- 외부 계약과 내부 계약의 변환은 `app/services` 경계에서 수행한다.
- LLM 판단이 필요한 부분과 코드로 확정해야 하는 부분을 분리한다.

## Known Gaps

- GitHub Actions에 독립적인 테스트 CI workflow는 없다. 현재 workflow는 배포·롤백만 수행한다.
- 루트 README와 일부 코드 docstring에는 제거된 DB 직접 접근 시대의 표현이 남아 있다. 실제 구현 판단에는 현재 client/runner/schema 코드를 우선한다.

## Update When

Python/runtime, 핵심 dependency, 패키지 책임, 외부 시스템 또는 서버의 상태 소유 경계가 의미 있게 바뀔 때 갱신한다.

## Validation

- `uv run python -c "import app.server; print(app.server.app.title)"`
- `rg --files app tests`
- `rg -n "dependencies|requires-python" pyproject.toml`
- `rg -n "include_router|add_middleware" app/server.py`
