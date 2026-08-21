# Timeline task 수명주기

## Scope

Timeline 생성 요청이 202로 접수된 뒤 입력 조회, Agent 실행, 결과 저장, callback으로 끝나는 실제 순서와 실패 분류를 설명한다.

## Read When

- `POST /v1/timeline`, `/invocations`, `/ping`을 수정할 때
- App Server 호출 순서, timeout, retry, callback 정책을 바꿀 때
- 배포 중 진행 task 보호나 task 완료 관측을 바꿀 때

## Authoritative Sources

- `app/api/v1/timeline.py`, `app/api/agentcore.py`
- `app/services/timeline_runner.py`, `app/services/app_server_client.py`
- `app/core/inflight.py`, `app/core/execution_context.py`
- `app/schemas/task.py`, `app/core/error_codes.py`
- `tests/api/test_timeline_endpoint.py`, `tests/api/test_agentcore_endpoint.py`
- `tests/services/test_timeline_runner.py`, `tests/services/test_app_server_client.py`

## Current Implementation

### 접수

`POST /v1/timeline`은 `taskId`, 최초 `taskToken`, `dailyRecordId`, timezone-aware `window.startAt/endAt`을 받는다. 요청 검증이 끝나면 `process_timeline_task`를 FastAPI `BackgroundTasks`에 등록하고 `202 {taskId, status: PROCESSING}`을 반환한다. AI 서버는 이후 상태 조회 API나 task record를 만들지 않는다.

`POST /invocations`는 AgentCore 고정 경로이며 같은 요청 모델과 handler를 호출한다. 처리 로직은 별도로 두지 않는다.

### 백그라운드 순서

1. `track_inflight`가 프로세스 로컬 처리 수를 증가시킨다.
2. 최초 token으로 task별 `TaskToken` holder를 만든다.
3. App Server에서 source snapshot을 조회하고 묶음 계약을 검증한다.
4. 접수 요청의 window로 snapshot window를 덮어쓴 뒤 도메인별로 정규화한다. App Server 응답 window가 아니라 접수 window가 정본이다.
5. main Agent 전체를 `PIPELINE_TIMEOUT_SEC`로 제한해 실행한다. Repair가 draft를 확정할 때마다 복사본을 runner로 발행하므로, 제한 시간이 끝나 실행이 취소돼도 마지막 확정본이 남는다. 그 확정본이 있으면 아래 6~8을 그대로 지나 SUCCESS로 끝나고, 없을 때만 `1201`로 실패한다.
6. 최종 draft가 현재 task의 rawId만 참조하는지 저장 전 검증한다.
7. 좁은 App Server 결과 계약으로 변환해 결과를 제출한다.
8. 결과 제출 성공 뒤에만 SUCCESS callback을 보낸다. 앞 단계 실패는 가능한 경우 FAILED callback을 보낸다.
9. Langfuse flush와 task당 하나의 `timeline.task.completed` 운영 이벤트를 남기고 inflight 수를 감소시킨다.

### 상태와 실패

`PROCESSING`은 접수 응답에만 쓰고 completion callback에는 `SUCCESS` 또는 `FAILED`만 허용한다. 성공 callback에는 error가 없어야 하고 실패 callback에는 카탈로그의 유효한 정수 `errorCode`와 정확히 대응하는 안전 메시지가 있어야 한다.

401, 404, 409 App Server 응답은 token/task/순서가 무효한 abort다. retry하지 않고 같은 이유로 거절될 callback도 보내지 않는다. 단, 입력 조회의 404는 `SOURCE_SNAPSHOT_NOT_FOUND`, 다른 경로의 404는 `APP_SERVER_TASK_NOT_FOUND`로 구분한다.

timeout·연결 계열 `httpx.HTTPError`와 5xx는 설정된 횟수까지 지수 backoff로 retry한다. 그 밖의 4xx는 즉시 실패하되 callback 가능한 실패로 취급한다. retry는 같은 token과 같은 request body를 사용한다.

callback 전송 실패는 저장된 결과나 최종 SUCCESS 상태를 되돌리지 않는다. 결과 저장 전에 발생한 실패만 FAILED가 될 수 있다.

### inflight와 health

`GET /ping`은 inflight가 0이면 `Healthy`, 하나 이상이면 `HealthyBusy`다. EC2 배포 스크립트는 기존 컨테이너가 `Healthy`가 될 때까지 최대 20분 기다린 뒤 교체한다. 이 counter는 task 상태 저장소가 아니며 taskId도 보관하지 않는다.

## Invariants

- 접수 202와 처리 완료를 같은 시점으로 해석하지 않는다.
- 입력 조회 → Agent → 결과 저장 → callback 순서를 바꾸지 않는다.
- 저장 성공 확인 전 SUCCESS callback 금지, 저장 성공 후 FAILED 전환 금지다.
- task token 값은 어떤 로그·관측·URL·outbound body에도 남기지 않는다.
- task 하나는 token holder 하나와 최신 token 하나를 공유한다.
- main Agent timeout만 `PIPELINE_TIMEOUT_SEC` 대상이다. App Server 호출은 각 호출의 timeout/retry 설정을 별도로 쓴다.
- timeout은 그 자체로 실패가 아니다. 저장할 확정 draft가 없을 때만 `1201` 실패다. 확정본을 저장한 경우는 SUCCESS이며 `timedOut`/`partialSave`로 구분한다 — `errorCode`는 비어 있다.
- `asyncio.wait_for`는 `asyncio.to_thread` 위의 LLM 호출을 끊지 못한다. 취소된 뒤에도 그 스레드는 계속 돌며 draft를 고치므로, 발행 값은 참조가 아니라 deep copy여야 한다.

## Known Gaps

- `TimelineWindowPayload`는 두 값이 aware datetime인지 검증하지만 `endAt >= startAt`을 endpoint schema에서 강제하지 않는다. 뒤의 window resolver는 역전된 범위 검증을 건너뛴다.
- FastAPI BackgroundTasks에는 durable queue, lease, 재시작 후 resume 기능이 없다.
- inflight는 프로세스 로컬이라 multi-worker·multi-replica 전체 busy 상태를 표현하지 못한다.
- callback 전송 실패 후 별도 보상 queue나 재처리 persistence는 없다. client는 실패를 `False`로 반환하고 task 완료 이벤트에 `callbackSent=false`를 남긴다.

## Update When

접수 payload·응답, 단계 순서, window 정본, timeout 범위, retry/abort 분류, terminal 상태, 결과 저장/callback 관계, inflight/health 의미가 달라질 때 갱신한다.

## Validation

- `uv run pytest tests/api/test_timeline_endpoint.py tests/api/test_agentcore_endpoint.py -q`
- `uv run pytest tests/services/test_timeline_runner.py tests/services/test_app_server_client.py -q`
- `rg -n "add_task|wait_for|submit_result|send_callback|abort_callback|track_inflight" app`

