# Codebase Knowledge Router

애플리케이션 구현을 수정할 때 사용하는 세부 Router다. 프로젝트 전체 원칙과 도메인·Git 문서는 [상위 인덱스](../README.md)에서 찾는다.

| Page | Read when | Related paths | Update when | Authority | Validate with |
|---|---|---|---|---|---|
| [프로젝트 개요](overview.md) | 기술 스택과 패키지 책임을 파악할 때 | `pyproject.toml`, `app/**`, `Dockerfile` | 핵심 런타임·의존성·패키지 책임·외부 시스템이 달라질 때 | 설정과 실행 코드 | import smoke, 파일 목록 |
| [아키텍처](architecture.md) | 계층 경계와 호출 방향을 바꿀 때 | `app/server.py`, `app/api/**`, `app/core/**`, `app/agents/**`, `app/services/**`, `app/schemas/**` | 상태 소유권, 컴포넌트 책임, 동기·비동기 경계가 달라질 때 | 코드와 관련 테스트 | API·main·service 테스트 |
| [제약](constraints.md) | 설계 선택 전에 금지·순서·운영 제약을 확인할 때 | `app/**`, `Dockerfile`, `.dockerignore`, `scripts/deploy-ec2.sh`, `tests/core/**` | 강제 제약이 추가·변경·해제될 때 | 코드·정적 테스트·배포 파일 | 제약별 테스트 |
| [변경 영향](change-impact.md) | 수정에 동반될 계약·테스트를 찾을 때 | `app/**`, `tests/**`, `.github/**` | 반복 변경 단위와 검증 묶음이 달라질 때 | 참조 관계와 테스트 구조 | `rg`, 대상 테스트 |
| [Timeline task 수명주기](runtime/timeline-task.md) | 접수부터 저장·callback까지를 바꿀 때 | `app/api/v1/timeline.py`, `app/api/agentcore.py`, `app/services/timeline_runner.py`, `app/services/app_server_client.py`, `app/core/inflight.py` | 처리 순서·상태·실패·callback/inflight 계약이 달라질 때 | runner/client 코드 | Timeline API/client/runner 테스트 |
| [Agent pipeline](runtime/agent-pipeline.md) | Agent graph와 Repair/guard를 바꿀 때 | `app/agents/**`, `app/services/draft_repair.py`, `app/services/*_guard.py` | node·병렬성·fallback·확정 순서가 달라질 때 | Agent/service 코드 | main/agents/repair 테스트 |
| [HTTP API](interfaces/http-api.md) | inbound 계약·오류·health를 바꿀 때 | `app/server.py`, `app/api/**`, `app/schemas/error.py`, `app/schemas/task.py` | endpoint·status·payload·인증·노출 의미가 달라질 때 | route와 Pydantic 모델 | `tests/api/**`, OpenAPI |
| [App Server 계약](interfaces/app-server.md) | 서버간 입력·결과·callback을 바꿀 때 | `app/services/app_server_client.py`, `app/services/timeline_runner.py`, `app/schemas/timeline_input.py`, `app/schemas/timeline_result.py`, `app/schemas/task.py` | path·body·Task-Token·retry/abort·호출 순서가 달라질 때 | client/runner/model | client/runner/result 테스트 |
| [LLM·프롬프트 계약](interfaces/llm-and-prompts.md) | provider와 prompt version을 바꿀 때 | `app/core/llm.py`, `app/core/structured.py`, `app/agents/prompt_loader.py`, `app/agents/**/prompts/**`, `app/core/config.py` | provider 기능·인증, structured output, prompt 세트가 달라질 때 | provider/loader/prompt 코드 | provider/structured/prompt 테스트 |
| [데이터 소유권·스키마](data/ownership-and-schema.md) | persistence/cache/schema/rawId를 바꿀 때 | `app/schemas/**`, `app/services/normalizer.py`, `app/services/source_contract.py`, `app/services/source_integrity.py`, `app/services/timeline_result.py` | 소유권·식별자·변환·저장 경계가 달라질 때 | 스키마와 변환 코드 | normalizer/source/result/validator 테스트 |
| [로컬 개발·테스트](operations/local-development-and-testing.md) | 개발 환경과 검증 명령을 고를 때 | `.python-version`, `pyproject.toml`, `uv.lock`, `tests/**`, `.gitignore`, `AGENTS.md` | 도구·버전·marker·live/CI 조건이나 테스트 임시물 정리가 달라질 때 | 설정, 테스트, agent 지침 | `uv sync --locked`, cache 비활성 pytest, 임시물 확인 |
| [배포](operations/deployment.md) | 이미지·EC2·AgentCore 절차를 바꿀 때 | `Dockerfile`, `.dockerignore`, `.github/workflows/**`, `scripts/deploy-ec2.sh`, `scripts/prune_ecr_images.py` | trigger·platform·idle/health·rollback·보존 정책이 달라질 때 | workflow/Dockerfile/script | script 테스트, Docker build |
| [관측성](operations/observability.md) | 로그·오류·trace·redaction을 바꿀 때 | `app/core/logging.py`, `app/core/operational_logging.py`, `app/core/langfuse_tracing.py`, `app/core/redaction.py`, `app/core/error_codes.py`, `docs/observability/**` | sink·수집 표식·허용 필드·본문 정책·오류 상관 규칙이 달라질 때 | 관측 코드와 Filebeat 설정 | core 관측·보안 테스트 |

## Router 유지 규칙

- 새 문서는 최소 두 종류 이상의 작업에서 반복해 읽을 가치가 있을 때만 추가한다.
- 한 파일의 세부 구현을 그대로 옮긴 문서는 만들지 않는다.
- 기존 페이지의 `Scope`에 자연스럽게 들어가는 지식은 새 페이지 대신 기존 페이지를 갱신한다.
