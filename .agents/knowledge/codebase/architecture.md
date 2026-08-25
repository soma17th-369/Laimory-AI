# 아키텍처

## Scope

FastAPI 접수 계층부터 App Server 연동과 Agent 실행까지의 컴포넌트 책임, 호출 방향, 상태 소유권과 동시성 경계를 설명한다.

## Read When

- route, background task, Agent graph, service 경계를 이동할 때
- 새 외부 연동이나 저장 경로를 추가할 때
- 로직이 `api`, `agents`, `services`, `core` 중 어디에 있어야 하는지 판단할 때

## Authoritative Sources

- `app/server.py`, `app/api/v1/timeline.py`, `app/api/v1/user_memory.py`, `app/api/agentcore.py`
- `app/services/timeline_runner.py`, `app/services/user_memory_runner.py`, `app/services/app_server_client.py`
- `app/agents/main/main_agent.py`, `app/agents/repair/repair_agent.py`
- `app/services/draft_repair.py`, `app/services/timeline_result.py`, `app/services/timeline_validator.py`
- `tests/api/**`, `tests/main/**`, `tests/services/**`

## Current Implementation

호출 방향은 `API → task runner → App Server/normalizer → main Agent → validator/result mapper → App Server`다. `schemas`는 모든 계층이 참조하지만 외부 I/O나 orchestration을 수행하지 않는다. `core`는 특정 도메인보다 설정·오류·LLM·관측 같은 횡단 관심사를 제공한다.

`POST /v1/timeline`은 요청을 검증하고 FastAPI `BackgroundTasks`에 `process_timeline_task`를 등록한 뒤 202를 반환한다. `/invocations`는 AgentCore adapter이며 `requestType`으로 Timeline과 User Memory를 갈라 각각의 `/v1` 핸들러를 그대로 재사용한다. 실제 비즈니스 파이프라인을 route에 중복 구현하지 않는다.

`timeline_runner`는 task 전체의 transaction-like 순서를 소유하지만 DB transaction은 아니다. App Server 입력 조회, 정규화, main Agent timeout, 저장 전 검증, 결과 제출, callback을 순서대로 연결하고 최종 상태·오류 코드를 한곳에서 확정한다.

`POST /v1/user-memory`(#64)는 같은 202 + background 형태를 쓰지만 **다른 task 종류**다. `user_memory_runner`가 기존 profile 해석, 입력 digest, 갱신 Agent, 크기·민감정보 확정, 결과 저장을 연결한다. Timeline과 달리 callback이 없어 지킬 순서 계약이 없고, 대신 **모든 실패 경로가 결과 저장 호출 하나로 수렴해야 한다**는 제약이 그 자리를 대신한다. 갱신 Agent는 main Agent graph의 노드가 아니며 `base.Agent`를 상속하지도 않는다 — 입력이 여러 날의 확정 기록이고 출력이 Timeline이 아니라, graph에 끼워 넣으면 "Timeline 단계 중 하나"로 읽힌다.

main Agent는 다음 단계로 구성된다.

1. 다섯 Event Agent를 `asyncio.to_thread`와 `asyncio.gather`로 병렬 실행한다.
2. Agent별 결과를 이름 키로 유지하면서 하나로 취합한다.
3. Timeline Agent가 draft를 만든다.
4. Repair Agent가 코드 확정 pass와 LLM 도구 개선을 반복한다.
5. Question Agent가 확정 event에 회고 질문을 붙인다.

LLM SDK 호출은 동기 provider wrapper이며 event loop를 막지 않도록 worker thread에서 실행된다. `contextvars` 실행 컨텍스트는 `asyncio.to_thread`에도 복사되어 taskId와 stage 상관관계를 유지한다.

제품 상태는 프로세스에 저장하지 않는다. 프로세스 로컬 상태는 설정 singleton, provider/client cache, prompt/module cache, notification dictionary cache, inflight counter와 관측 누산기뿐이다. 이 값들은 제품 persistence나 task 조회 원천이 아니다.

## Invariants

- API adapter끼리 비즈니스 로직을 복제하지 않는다. `/invocations`는 `requestType`에 따라 `/v1/timeline` 또는 `/v1/user-memory`와 같은 처리에 위임한다.
- `timeline_runner`·`user_memory_runner`보다 아래 계층이 SUCCESS/FAILED task 상태를 소유하지 않는다.
- `app_server_client`만 서버간 header·retry·status 해석을 소유한다.
- User Memory 갱신은 Timeline graph에 편입하지 않는다. 두 task는 trace 이름·operational event·timeout 예산이 각각이다.
- Repair 이후 event 구성이 확정된 다음에 event별 회고 질문을 생성한다.
- 결정론 검증·보정은 LLM이 특정 도구를 선택해야만 실행되는 구조로 만들지 않는다.

## Known Gaps

- 프로세스 로컬 inflight counter 때문에 multi-worker나 여러 replica 전체의 busy 상태를 합산하지 못한다.
- FastAPI `BackgroundTasks`는 durable queue가 아니다. 프로세스가 종료되면 실행 중 task를 다른 worker가 이어받는 구현은 없다.
- task 상태 조회 endpoint와 서버 내부 task persistence는 의도적으로 없다. 상태 복구와 재처리는 App Server 책임이지만 이 저장소에서 그 구현은 검증할 수 없다.

## Update When

컴포넌트 책임, 호출 방향, 상태 소유자, background 처리 방식, Agent graph 또는 thread/async 경계가 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/api tests/main tests/services/test_timeline_runner.py -q`
- `rg -n "add_task|process_timeline_task|run_main_agent|StateGraph|to_thread|gather" app`
- `rg -n "sqlalchemy|redis|celery" app pyproject.toml`
