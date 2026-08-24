# Laimory-AI Knowledge Index

이 디렉터리는 Coding Agent가 구현 전에 필요한 계약만 선택해 읽고, 구현 뒤 의미가 달라진 문서만 갱신하기 위한 지식 인덱스다. 전체 문서를 매번 읽는 용도가 아니다.

- 작성 기준일: 2026-08-06 (Asia/Seoul)
- 기준 HEAD: `3341b82282b31a9a4a09652a87afd0391da18b34`
- 조사 기준: 2026-08-05~2026-08-06에 위 HEAD의 코드·설정·스키마·테스트·workflow를 확인했다. 조사 도중 이슈 #66 Question Agent 변경과 Knowledge Workflow가 commit에 반영되어 최종 HEAD 기준 구현에 포함됐다.
- 최종 권위: 실행 코드, 설정 모델, Pydantic 스키마, 테스트, Dockerfile, 스크립트, GitHub Actions workflow
- 보조 자료: 루트 `README.md`, `AGENTS.md`, `docs/`. 보조 자료가 코드와 충돌하면 코드와 테스트를 따른다.
- 비권위 자료: `.agents/plans/`, `.agents/worklog/`, 세션 메모와 raw note. 구현 계약의 근거로 사용하지 않는다.

## 사용법

1. 변경하려는 경로가 `Related paths`에 걸리는 행을 찾는다.
2. 그 행의 `Read when`이 작업과 맞는 문서만 읽는다.
3. 구현 후 같은 행의 `Update when`을 확인한다. 경로가 바뀌었더라도 의미 변화가 없으면 문서를 고치지 않는다.
4. 문서와 코드가 다르면 코드·설정·스키마·테스트·workflow를 우선하고, 의미가 달라졌을 때만 문서를 같은 변경에서 갱신한다.

## 상태 표기

- `Current Implementation`: 기준 HEAD에서 실제로 동작하는 현재 구현이다.
- `Invariants`: 코드·설정·prompt·테스트가 의도하고 강제하는 목표 계약이다.
- `Known Gaps`: 구현되지 않았거나 이 저장소만으로 확인할 수 없는 항목이다. 일반적인 개선 제안을 추측해서 넣지 않는다.

## Router

| Page | Read when | Related paths | Update when | Authority | Validate with |
|---|---|---|---|---|---|
| [Codebase Router](codebase/README.md) | 구현 위치나 세부 지식 문서를 고를 때 | `app/**`, `tests/**`, `.github/workflows/**`, `Dockerfile`, `pyproject.toml` | codebase 하위 문서를 추가·이동·삭제하거나 Router 조건이 달라질 때 | 이 인덱스와 실제 하위 파일 | 링크 검사, `rg --files .agents/knowledge/codebase` |
| [프로젝트 개요](codebase/overview.md) | 기술 스택, 패키지 책임, 외부 시스템을 빠르게 파악할 때 | `pyproject.toml`, `app/**`, `Dockerfile` | 런타임, 핵심 의존성, 최상위 모듈 책임이나 외부 시스템 경계가 바뀔 때 | `pyproject.toml`, `app/server.py`, 패키지 코드 | import smoke, `rg --files app` |
| [아키텍처](codebase/architecture.md) | 계층 경계, 의존 방향, task/Agent 처리 구조를 바꿀 때 | `app/server.py`, `app/api/**`, `app/core/**`, `app/agents/**`, `app/services/**`, `app/schemas/**` | 컴포넌트 책임, 호출 방향, 상태 소유권, 동기·비동기 경계가 바뀔 때 | 실행 코드와 아키텍처 테스트 | 관련 `tests/api`, `tests/main`, `tests/services` |
| [제약](codebase/constraints.md) | 구현 선택이 운영·보안·데이터 경계를 건드릴 수 있을 때 | `app/**`, `Dockerfile`, `.dockerignore`, `scripts/deploy-ec2.sh`, `tests/core/**` | 금지·순서·단일 프로세스·보안 제약이 추가되거나 해제될 때 | 코드, 정적 방어 테스트, 배포 파일 | 제약별 테스트와 diff 검토 |
| [변경 영향](codebase/change-impact.md) | 수정 경로가 어떤 계약·테스트·문서를 함께 요구하는지 판단할 때 | `app/**`, `tests/**`, `.github/**`, `docs/**` | 반복적으로 함께 바뀌는 경계나 검증 세트가 달라질 때 | 실제 import/call 관계와 테스트 배치 | `rg` 참조 검색, 대상 테스트 |
| [Timeline task 수명주기](codebase/runtime/timeline-task.md) | 접수, 백그라운드 처리, 저장, 콜백, 실패 순서를 바꿀 때 | `app/api/v1/timeline.py`, `app/api/agentcore.py`, `app/services/timeline_runner.py`, `app/services/app_server_client.py`, `app/core/inflight.py` | 단계 순서, timeout, terminal 상태, callback·abort·inflight 의미가 바뀔 때 | runner/client 코드와 API·service 테스트 | Timeline API/client/runner 테스트 |
| [Agent pipeline](codebase/runtime/agent-pipeline.md) | Event/Timeline/Repair/Question Agent나 guard 순서를 바꿀 때 | `app/agents/**`, `app/services/draft_repair.py`, `app/services/*_guard.py`, `app/services/source_integrity.py`, `app/services/validator.py` | 그래프 노드, 병렬성, fallback, Repair 반복·도구, 확정 pass 순서가 바뀔 때 | Agent·service 코드, prompt와 테스트 | main/agents/draft repair 테스트 |
| [HTTP API](codebase/interfaces/http-api.md) | inbound endpoint, 요청·응답·오류·health 계약을 바꿀 때 | `app/server.py`, `app/api/**`, `app/schemas/error.py`, `app/schemas/task.py` | 경로, 상태 코드, payload 의미, 오류 형태, 인증·health 노출이 바뀔 때 | FastAPI route와 Pydantic 모델 | `tests/api/**`, OpenAPI 수동 확인 |
| [App Server 계약](codebase/interfaces/app-server.md) | 입력 조회·결과 저장·콜백·Task-Token 연동을 바꿀 때 | `app/services/app_server_client.py`, `app/services/timeline_runner.py`, `app/schemas/timeline_input.py`, `app/schemas/timeline_result.py`, `app/schemas/task.py` | 경로, body, token 갱신, retry·abort, 저장/callback 순서가 바뀔 때 | client/runner와 계약 모델 | App Server client/runner/result 테스트 |
| [LLM·프롬프트 계약](codebase/interfaces/llm-and-prompts.md) | provider, 모델 호출, structured output, prompt version을 바꿀 때 | `app/core/llm.py`, `app/core/secrets.py`, `app/core/structured.py`, `app/agents/prompt_loader.py`, `app/agents/**/prompts/**`, `app/core/config.py` | provider 인증·기능, 공통 prompt version, 활성 파일, structured retry가 바뀔 때 | provider/loader 코드와 prompt | provider/structured/prompt 테스트 |
| [데이터 소유권·스키마](codebase/data/ownership-and-schema.md) | DB, 저장, cache, rawId, 내부·전송 스키마 경계를 바꿀 때 | `app/schemas/**`, `app/services/normalizer.py`, `app/services/source_contract.py`, `app/services/source_integrity.py`, `app/services/timeline_result.py`, `app/services/timeline_validator.py` | 데이터 소유자, persistence/cache 유무, 식별자·변환·검증 규칙이 바뀔 때 | 모델과 변환·검증 코드 | normalizer/source/result/validator 테스트 |
| [로컬 개발·테스트](codebase/operations/local-development-and-testing.md) | 환경 구성, 실행, 의존성, 테스트 전략을 다룰 때 | `.python-version`, `pyproject.toml`, `uv.lock`, `tests/**`, `.gitignore`, `AGENTS.md` | Python/uv 절차, marker, 기본·live 테스트 조건, 테스트 임시물 정리나 CI 존재 여부가 바뀔 때 | 버전·의존성 설정, 테스트, agent 지침 | `uv sync --locked`, cache 비활성 pytest, 임시물 확인 |
| [배포](codebase/operations/deployment.md) | Docker, EC2, ECR, SSM, AgentCore 배포·롤백을 바꿀 때 | `Dockerfile`, `.dockerignore`, `.github/workflows/**`, `scripts/deploy-ec2.sh`, `scripts/prune_ecr_images.py` | 기본 배포 대상, architecture, trigger, health/idle, rollback, 이미지 보존 정책이 바뀔 때 | workflow, Dockerfile, 배포 스크립트 | script 테스트, Docker build |
| [관측성](codebase/operations/observability.md) | 로그, 오류 코드, Elasticsearch/Filebeat, Langfuse, redaction을 바꿀 때 | `app/core/logging.py`, `app/core/operational_logging.py`, `app/core/langfuse_tracing.py`, `app/core/redaction.py`, `app/core/error_codes.py`, `docs/observability/**` | 수집 경계, event 필드, content 정책, 오류 상관 규칙이나 sink가 바뀔 때 | 관측 코드, Filebeat 설정, 보안 테스트 | core 관측·보안 테스트 |
| [공통 언어](domain/ubiquitous-language.md) | 도메인 이름·API 용어·모델 이름을 만들거나 바꿀 때 | `app/schemas/**`, `app/agents/**`, `app/services/**`, `docs/ai-*.md`, `docs/timeline-source-item.md` | 공식 이름·개념 경계·서로 다른 질문 유형이 달라질 때 | 스키마 이름, enum, 실제 호출부 | `rg` 용어 사용처와 모델 테스트 |
| [도메인 불변식](domain/invariants.md) | 결과 의미, 근거, 시간, 문장, 오류·보안 규칙을 바꿀 때 | `app/schemas/**`, `app/services/**`, `app/agents/**/prompts/**`, `tests/**` | 코드·prompt·테스트가 강제하는 도메인 규칙이 추가·변경·삭제될 때 | validator/guard, prompt, 회귀 테스트 | 불변식별 테스트 |
| [브랜치 관례](conventions/branch.md) | 브랜치를 만들거나 이슈와 연결할 때 | `.git/config`, Git refs, `.github/ISSUE_TEMPLATE/**`, 최근 merge 이력 | 관찰되는 prefix·이슈 연결·기본 브랜치 관례가 달라질 때 | 실제 refs와 merge 기록 | `git branch -a`, `git log --merges` |
| [이슈 관례](conventions/issue.md) | Issue를 생성·수정하거나 Type·Priority·Size·Epic 분해를 정할 때 | `.github/ISSUE_TEMPLATE/**`, `.agents/skills/create-issue/**`, `AGENTS.md` | 제목 아이콘·Type prefix, 본문 템플릿, 필드, sub-issue 연결 워크플로우가 바뀔 때 | GitHub Issue 템플릿, create-issue 스킬, 실제 Issue 이력 | 템플릿 frontmatter, `rg`, GitHub Issue 제목 |
| [커밋 관례](conventions/commit.md) | commit 메시지를 작성하거나 PR용 변경을 작은 작업 단위로 나눌 때 | Git history, `AGENTS.md`, `.agents/knowledge/conventions/pull-request.md` | commit prefix·형식, 분할·완결성 원칙, merge 방식이 달라질 때 | 최근 non-merge·merge commit, 실제 diff | `git log --no-merges`, `git log --merges`, staged diff |
| [PR 관례](conventions/pull-request.md) | PR을 작성·검토·갱신하거나 기존 본문 항목과 commit 구성을 확인할 때 | `.github/pull_request_template.md`, `AGENTS.md`, Git history, GitHub PR | PR 제목, base·head, 이슈 연결, 본문 섹션, commit·merge 방식이 바뀔 때 | PR template, 실제 PR·merge 이력 | PR template, `git log --merges`, GitHub PR 필드 |

## 갱신 원칙

- `Related paths`는 읽을 후보를 찾는 색인이다. 그 경로가 바뀌었다는 사실만으로 문서를 갱신하지 않는다.
- `Update when`의 계약·의미·운영 방식이 달라졌을 때만 코드 변경과 같은 변경 묶음에서 갱신한다.
- 기준 commit을 바꾸기 위한 단독 갱신은 하지 않는다. 의미 갱신이 있을 때 새 기준을 함께 기록한다.
- 실제 secret, credential, token, 사용자 원문은 기록하지 않는다. 환경변수 이름과 안전한 placeholder만 허용한다.
