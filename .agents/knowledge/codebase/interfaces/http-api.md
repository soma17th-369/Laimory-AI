# HTTP API 계약

## Scope

AI 서버가 직접 노출하는 inbound HTTP 경로, 요청·응답·오류·health 계약과 현재 인증 경계를 설명한다.

## Read When

- FastAPI route나 payload를 추가·변경할 때
- 오류 response, OpenAPI, request logging을 바꿀 때
- health/debug endpoint 또는 inbound 인증을 검토할 때

## Authoritative Sources

- `app/server.py`, `app/api/v1/router.py`, `app/api/v1/timeline.py`, `app/api/v1/timeline_testing.py`, `app/api/v1/user_memory.py`, `app/api/agentcore.py`
- `app/api/error_handlers.py`, `app/api/request_logging.py`
- `app/schemas/error.py`, `app/schemas/task.py`, `app/schemas/user_memory_update.py`
- `tests/api/**`

## Current Implementation

| 경로 | 의미 | 성공 계약 |
|---|---|---|
| `POST /v1/timeline` | Timeline task 일반 접수 | 202, `taskId`와 `PROCESSING`; 처리는 background에서 계속됨 |
| `POST /v1/user-memory` | User Memory 갱신 접수 (#64) | 202, `taskId`와 `PROCESSING`; 결과는 App Server 저장 호출 한 번으로 통보 |
| `POST /v1/timeline/test` | 동기 Timeline 생성 (테스트 전용, #102) | 200, App Server 결과 저장 요청과 **같은 body**; 접수가 아니라 완료다 |
| `POST /invocations` | AgentCore 고정 호출 경로 | `{requestType, payload}` envelope로 Timeline과 User Memory를 모두 접수하고 같은 handler에 위임. 202, `taskId`와 `PROCESSING` |
| `GET /ping` | AgentCore·배포 health | 200, `Healthy` 또는 `HealthyBusy`; inflight만 확인 |
| `GET /health` | 단순 process health | 200, `{"status":"ok"}` |
| `GET /debug/env` | 일부 설정 존재 여부 진단 | `APP_ENV`와 OpenAI key 존재 여부 boolean; key 값은 반환하지 않음 |

FastAPI 기본 `/docs`, `/redoc`, `/openapi.json`도 별도 비활성 설정 없이 노출된다.

Timeline 접수 request는 빈 문자열이 아닌 `taskId`·`taskToken`, 정수 `dailyRecordId`, timezone-aware `window.startAt/endAt`을 요구한다. taskToken은 이 inbound body에서 최초 값으로만 받고 outbound App Server 인증 header로 옮긴다.

User Memory 접수 request는 `taskId`·`taskToken`과 optional `userMemory`, `dailyTimelines`를 받는다. `dailyTimelines`는 App Server 재시도 배치 계약에 따라 최대 5건이며 6건 이상은 422/1001이다. 그 안의 계약은 **일부러 느슨하다** — `eventType`은 자유 문자열이고 `endAt`·`subtitle`·`question`·`memo`·`emotionType`은 nullable이며 event 수와 본문 길이 상한을 schema에서 강제하지 않는다. 이벤트가 많은 정상적인 하루는 prompt 조립 단계에서 자른다. 기존 `userMemory`는 원본 dict로 받고 background에서 따로 검증한다 — 여기서 엄격히 선언하면 읽지 못하는 프로필 하나가 접수 자체를 막고, 그 사용자는 이후 어떤 날도 갱신되지 않는다.

`/invocations`는 AgentCore가 컨테이너에 진입점을 하나만 요구하기 때문에 요청 종류를 body 최상위 `requestType`(`TIMELINE`, `USER_MEMORY_UPDATE`)으로 받는다(#89). `payload`는 해당 `/v1` 엔드포인트의 request body 그대로이며 필드를 선별하거나 이름을 바꾸지 않는다. `requestType`이 `payload` schema를 결정하는 discriminated union이라 payload 필드 모양으로 종류를 추측하지 않는다 — `taskId`·`taskToken`은 양쪽에 있고 나머지는 optional이라 모양으로 판별하면 생략된 필드 하나가 요청을 다른 pipeline으로 보낸다. `requestType` 키가 없는 body는 Timeline 직접 payload로 감싸며, 이는 **제거 예정이 아닌 두 번째 정식 형식**이다. 판별은 키 존재 여부만 보고 payload 내부는 읽지 않는다.

`POST /v1/timeline/test`는 유일한 **동기** 경로다(#102). 만든 결과를 어디에도 저장하지 않고 응답으로만 내보낸다.

request schema는 **입력 조회 응답과 필드 선언을 공유한다**. `app/schemas/timeline_input.py`가 두 겹으로 나뉘어, `TimelineInputPayload`가 입력 한 벌(`taskId`·`recordDate`·`recordTimeZone`·`window`·`userMemory`·`sourceItems`)을 선언하고 `TimelineInputResponse`가 거기에 `taskToken`만 더한다. 이 endpoint의 `TimelineTestRequest`는 앞의 것을 상속하고 `window`를 필수로 좁히는 한 줄만 더한다 — 필드를 손으로 다시 적으면 한쪽만 고쳐져 두 입구가 말없이 갈린다.

쪼갠 기준은 "AI 서버가 App Server를 **되부를 때** 쓰는 값인가"다. `taskId`는 App Server가 발행해 AI 서버가 **받는** 값이라 두 입구에 공통이고, `taskToken`은 되부르는 호출의 인증이라 되부르지 않는 입구에는 쓸 곳이 없다 — 그래서 토큰만 계약에서 빠진다(보내면 무시한다). 동기 경로는 `taskId`로 무엇을 조회하거나 저장하지 않지만, 같은 taskId로 돌린 비동기 실행과 로그·Langfuse에서 이어 볼 수 있어야 하고 `execution_context`(#98 guard가 읽는다)와 `CollectedSnapshot`도 그 값을 요구한다.

response body는 결과 저장 요청 계약 그대로다 — "저장될 값"을 보여 주는 것이 목적이라 필드를 더하거나 빼지 않는다. 제한 시간 초과로 마지막 확정본을 돌려줬다는 사실도 그래서 body가 아니라 `X-Timeline-Timed-Out` 응답 header로 나간다. 노출은 `settings.timeline_test_endpoint_enabled`(기본 `local`/`dev`, `TIMELINE_TEST_ENABLED`가 이김)가 정하며 두 겹이다 — 비활성이면 dependency가 던지는 404/1003이 요청마다 막고, `include_in_schema`가 OpenAPI에서도 감춘다. 403이 아니라 404인 것은 운영에 테스트 경로가 있다는 사실 자체를 알리지 않기 위해서다.

**`APP_ENV` 기본값만으로 "dev 서버에서 열려 있다"고 결론짓지 않는다.** EC2(개발) 인스턴스의 `/opt/laimory-ai/runtime.env`는 `APP_ENV=prod`로 운영되므로 거기서는 `TIMELINE_TEST_ENABLED=true`를 명시해야 열린다(`docs/deploy-ec2.md`). 같은 이유로 #48에서 Langfuse 본문 캡처가 dev 취급을 받지 못했다 — `APP_ENV`로 갈리는 기본값을 새로 만들 때마다 이 인스턴스가 예외가 된다.

모든 실패 response는 `ErrorResponse {errorCode: int, error: string}` 한 형태로 통일한다. request validation은 422/1001, route 404는 1003, method 405는 1004, 그 밖의 4xx는 1002, 미처리 5xx는 1901을 쓴다. `error`는 카탈로그의 외부 안전 메시지이며 validation input과 원본 exception은 response에 포함하지 않는다.

RequestLoggingMiddleware는 response header가 시작되는 시점에 요청당 운영 이벤트 한 건을 남긴다. BackgroundTasks 전체 시간을 HTTP latency에 포함하지 않는다. 정상 `/ping`, `/health` 요청은 로그 소음을 피하려고 수집하지 않지만 실패는 수집한다. query string과 임의 path 원문을 그대로 적재하지 않는다.

## Invariants

- `/invocations`는 처리 구현을 갖지 않고 `/v1` handler에 위임한다. 두 경로의 계약이 갈라지지 않는다.
- `/v1` POST **접수** 경로는 전부 `requestType`으로 도달할 수 있다. 접수 endpoint를 추가하면 `InvocationRequestType`도 함께 늘어난다(`tests/api/test_agentcore_endpoint.py`의 coverage guard가 강제한다). health·diagnostic 경로와 `_NON_INTAKE_V1_ROUTES`에 선언한 비접수 경로는 대상이 아니다 — `/v1/timeline/test`는 202 접수가 아니라 동기 완료라 `/invocations`의 접수 응답 계약에 담기지 않고, production에서는 닫혀 있어 운영 진입점에 실릴 이유가 없다.
- 테스트 전용 경로는 기본이 **닫힘**이다. 열려면 `local`/`dev`이거나 `TIMELINE_TEST_ENABLED`를 명시해야 한다. 모르는 `APP_ENV` 값에서는 저절로 닫힌다.
- 같은 입력 데이터를 받는 두 입구는 field를 각자 선언하지 않는다. `TimelineInputPayload`를 상속해 한 선언을 공유하고, 그 입구에만 해당하는 것(필수 여부 좁히기, task 배관)만 각자 더한다.
- `/v1/timeline`과 `/v1/user-memory`는 AgentCore 전환 뒤에도 계속 열어 둔다. App Server는 HTTP 직접 호출과 `InvokeAgentRuntime` 두 경로를 모두 쓴다.
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

