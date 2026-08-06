# HTTP API 계약

## Scope

AI 서버가 직접 노출하는 inbound HTTP 경로, 요청·응답·오류·health 계약과 현재 인증 경계를 설명한다.

## Read When

- FastAPI route나 payload를 추가·변경할 때
- 오류 response, OpenAPI, request logging을 바꿀 때
- health/debug endpoint 또는 inbound 인증을 검토할 때

## Authoritative Sources

- `app/server.py`, `app/api/v1/router.py`, `app/api/v1/timeline.py`, `app/api/v1/user_memory.py`, `app/api/agentcore.py`
- `app/api/error_handlers.py`, `app/api/request_logging.py`
- `app/schemas/error.py`, `app/schemas/task.py`, `app/schemas/user_memory_update.py`
- `tests/api/**`

## Current Implementation

| 경로 | 의미 | 성공 계약 |
|---|---|---|
| `POST /v1/timeline` | Timeline task 일반 접수 | 202, `taskId`와 `PROCESSING`; 처리는 background에서 계속됨 |
| `POST /v1/user-memory` | User Memory 갱신 접수 (#64) | 202, `taskId`와 `PROCESSING`; 결과는 App Server 저장 호출 한 번으로 통보 |
| `POST /invocations` | AgentCore 고정 호출 경로 | `/v1/timeline`과 같은 request/response와 handler 사용. User Memory는 받지 않는다 |
| `GET /ping` | AgentCore·배포 health | 200, `Healthy` 또는 `HealthyBusy`; inflight만 확인 |
| `GET /health` | 단순 process health | 200, `{"status":"ok"}` |
| `GET /debug/env` | 일부 설정 존재 여부 진단 | `APP_ENV`와 OpenAI key 존재 여부 boolean; key 값은 반환하지 않음 |

FastAPI 기본 `/docs`, `/redoc`, `/openapi.json`도 별도 비활성 설정 없이 노출된다.

Timeline 접수 request는 빈 문자열이 아닌 `taskId`·`taskToken`, 정수 `dailyRecordId`, timezone-aware `window.startAt/endAt`을 요구한다. taskToken은 이 inbound body에서 최초 값으로만 받고 outbound App Server 인증 header로 옮긴다.

User Memory 접수 request는 `taskId`·`taskToken`과 optional `userMemory`, `dailyTimelines`를 받는다. `dailyTimelines`는 App Server 재시도 배치 계약에 따라 최대 5건이며 6건 이상은 422/1001이다. 그 안의 계약은 **일부러 느슨하다** — `eventType`은 자유 문자열이고 `endAt`·`subtitle`·`question`·`memo`·`emotionType`은 nullable이며 event 수와 본문 길이 상한을 schema에서 강제하지 않는다. 이벤트가 많은 정상적인 하루는 prompt 조립 단계에서 자른다. 기존 `userMemory`는 원본 dict로 받고 background에서 따로 검증한다 — 여기서 엄격히 선언하면 읽지 못하는 프로필 하나가 접수 자체를 막고, 그 사용자는 이후 어떤 날도 갱신되지 않는다.

모든 실패 response는 `ErrorResponse {errorCode: int, error: string}` 한 형태로 통일한다. request validation은 422/1001, route 404는 1003, method 405는 1004, 그 밖의 4xx는 1002, 미처리 5xx는 1901을 쓴다. `error`는 카탈로그의 외부 안전 메시지이며 validation input과 원본 exception은 response에 포함하지 않는다.

RequestLoggingMiddleware는 response header가 시작되는 시점에 요청당 운영 이벤트 한 건을 남긴다. BackgroundTasks 전체 시간을 HTTP latency에 포함하지 않는다. 정상 `/ping`, `/health` 요청은 로그 소음을 피하려고 수집하지 않지만 실패는 수집한다. query string과 임의 path 원문을 그대로 적재하지 않는다.

## Invariants

- `/invocations`와 `/v1/timeline`의 Timeline 계약과 처리 구현은 갈라지지 않는다.
- 202는 완료가 아니라 접수다. 최종 상태는 Timeline이면 App Server callback, User Memory면 결과 저장 호출로 통보한다.
- User Memory 접수는 `dailyTimelines` 5건 상한을 넘으면 422/1001을 내고, 그 안의 event 수와 본문 길이는 background에서 자른다.
- API 오류에 validation input, token, 원본 exception message를 반환하지 않는다.
- route별 임의 오류 body를 만들지 않고 전역 handler를 사용한다.

## Known Gaps

- inbound API key, bearer token, signature, principal 또는 role 검증 코드가 없다. 배포 network/AgentCore가 접근을 제한하는지는 이 저장소에서 증명할 수 없다.
- `/debug/env`에 운영 환경 비활성화나 인증 제한이 없다.
- `/health`와 `/ping`은 App Server·LLM·Langfuse 연결을 확인하지 않는다. process/inflight health만 나타낸다.
- 접수 window의 시작·종료 순서 검증이 없다.

## Update When

경로, method, status, request/response 의미, 공통 오류 모양, OpenAPI 노출, request event 시점, inbound 인증·health 범위가 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/api -q`
- 서버 실행 후 `/openapi.json`에서 path와 response schema 확인
- `rg -n "@(app|router)\.(get|post|put|delete|patch)|add_exception_handler|add_middleware" app`

