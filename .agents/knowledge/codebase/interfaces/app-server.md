# App Server 서버간 계약

## Scope

AI 서버가 App Server에서 source를 조회하고 Timeline 결과와 완료 상태를 보내는 세 HTTP 계약, Task-Token 인증, retry·abort 규칙을 설명한다.

## Read When

- App Server endpoint, payload, status 처리나 token 규칙을 바꿀 때
- 새로운 서버간 호출을 추가할 때
- 저장·callback 실패와 retry를 진단할 때

## Authoritative Sources

- `app/services/app_server_client.py`, `app/services/timeline_runner.py`
- `app/core/config.py`, `app/core/error_codes.py`
- `app/schemas/timeline_input.py`, `app/schemas/timeline_result.py`, `app/schemas/task.py`
- `app/services/source_contract.py`, `app/services/timeline_result.py`, `app/services/timeline_validator.py`
- `tests/services/test_app_server_client.py`, `test_timeline_runner.py`, `test_timeline_result.py`, `test_timeline_validator.py`

## Current Implementation

`APP_SERVER_API_URL`은 필수이며 절대 HTTP(S) URL이어야 한다. query/fragment는 허용하지 않고 path가 `/s/api/v{숫자}` 또는 `/s/v{숫자}`로 끝나야 한다. task path는 client가 아래처럼 조립하고 taskId를 URL encode한다.

| Operation | Method/path | 전송 의미 |
|---|---|---|
| input | `GET /timeline/drafts/{taskId}/input` | source snapshot 조회 |
| result | `POST /timeline/drafts/{taskId}/result` | 확정 event 목록 저장 |
| callback | `POST /timeline/drafts/{taskId}/callback` | SUCCESS/FAILED 상태만 통보 |

모든 요청은 현재 `TaskToken` 값을 `Task-Token` header에 싣는다. 최초 token은 inbound Timeline body에서 받고, 성공 response body에 다른 비어 있지 않은 `taskToken`이 있으면 holder를 갱신한다. 실패 response의 token은 흡수하지 않는다. token 값은 logging/tracing 대상이 아니며 `TaskToken.__str__`과 `repr`도 값을 가린다.

input response는 taskId, record date/timezone, optional window, 평평한 `sourceItems`와 optional 새 token을 제공한다. 이를 내부 `CollectedSnapshot`으로 변환한 뒤 다음 묶음 규칙을 검사한다.

- response taskId가 요청 taskId와 같음
- source item이 한 건 이상임
- 한 task 안에서 rawId가 유일함

접수 request window가 뒤에서 input response window를 덮어쓴다.

`userMemory`는 input 계약의 선택 필드다(#65). 응답에서는 원본 dict로 받고 `parse_user_memory()`가 `UserMemory` v1.0으로 따로 검증한다. 검증에 성공한 값만 `to_snapshot(user_memory=...)`으로 내부 snapshot에 들어간다. 필드가 없거나 `null`이면 `None`이다.

계약 위반(모르는 최상위 필드, 지원하지 않는 `schemaVersion`, 길이·개수 초과)은 묶음 규칙과 달리 **task를 실패시키지 않는다**. client가 code 1106으로 기록하고 memory 없이 진행한다. 응답 model에 `UserMemory`를 직접 선언하지 않는 이유가 이것이다 — 직접 선언하면 보조 context 하나가 응답 전체를 1102로 만든다.

result request는 내부 draft보다 좁다. App Server에는 event type, title, subtitle, 시작·종료, source rawId 목록과 optional event question만 보낸다. confidence, inference level, uncertainty, 내부 questions/warnings, address/place/tags, clientEventId는 보내지 않는다. title/subtitle/question은 mapper에서 최대 255자로 방어하고, source rawId는 순서를 유지해 중복 제거하며, datetime은 draft timezone offset으로 보낸다. event가 0건이어도 결과 request를 전송한다.

callback body는 terminal status와 오류 필드뿐이다. taskId는 URL, token은 header에 있으므로 body에 반복하지 않는다. SUCCESS는 error가 모두 null이고 FAILED는 예약되지 않은 카탈로그 code와 정확한 안전 메시지 쌍이어야 한다.

각 실제 request는 새 `httpx.AsyncClient`를 사용한다. singleton client가 서로 다른 event loop에 묶인 connection을 재사용하지 않기 위한 구현이다.

## Invariants

- App Server 접근 정책은 `app_server_client.py` 한곳에서 소유한다.
- token은 header 전용이고 값은 관측하지 않는다. 갱신 횟수만 허용한다.
- 2xx만 성공이다. 빈/non-JSON 2xx body는 허용하며 result 성공 body는 없어도 된다.
- timeout·transport error·5xx만 retry한다. 401/404/409는 abort, 다른 4xx는 callback 가능한 실패다.
- 저장 전 rawId 소속·필수값을 로컬 검증한다.
- result 성공 뒤에만 SUCCESS callback을 보낸다.

## Known Gaps

- App Server 측 DB schema, transaction, idempotency key, task state machine은 이 저장소에 없어 검증할 수 없다.
- POST result/callback은 transport timeout 뒤 같은 body로 retry하지만 별도 idempotency header는 없다. 서버 측 중복 안전성은 이 저장소에서 확인할 수 없다.
- input response `userMemory`를 소비할 준비는 됐지만, App Server가 실제로 이 필드를 채워 보내는지는 이 저장소에서 확인할 수 없다.
- callback 실패를 영속적으로 재시도하는 queue가 없다.

## Update When

base URL 규칙, operation path/method, request·response shape, Task-Token 전달·갱신, retry/abort status, 결과 narrowing, 저장/callback 순서가 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/services/test_app_server_client.py tests/services/test_timeline_runner.py -q`
- `uv run pytest tests/services/test_source_contract.py tests/services/test_timeline_result.py tests/services/test_timeline_validator.py -q`
- `uv run pytest tests/core/test_config.py -q`
- `rg -n "INPUT_PATH|RESULT_PATH|CALLBACK_PATH|TASK_TOKEN_HEADER|_ABORT_STATUSES" app/services/app_server_client.py`
