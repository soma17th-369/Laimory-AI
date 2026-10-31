# App Server 서버간 계약

## Scope

AI 서버가 App Server에서 source를 조회하고 Timeline 결과·완료 상태·User Memory 갱신 결과를 보내는 네 HTTP 계약, Task-Token 인증, retry·abort 규칙을 설명한다.

## Read When

- App Server endpoint, payload, status 처리나 token 규칙을 바꿀 때
- 새로운 서버간 호출을 추가할 때
- 저장·callback 실패와 retry를 진단할 때

## Authoritative Sources

- `app/services/app_server_client.py`, `app/services/timeline_runner.py`, `app/services/user_memory_runner.py`
- `app/core/config.py`, `app/core/error_codes.py`
- `app/schemas/timeline_input.py`, `app/schemas/timeline_result.py`, `app/schemas/task.py`, `app/schemas/user_memory_update.py`
- `app/services/source_contract.py`, `app/services/timeline_result.py`, `app/services/timeline_validator.py`
- `tests/services/test_app_server_client.py`, `test_timeline_runner.py`, `test_timeline_result.py`, `test_timeline_validator.py`, `test_user_memory_runner.py`

## Current Implementation

`APP_SERVER_API_URL`은 필수이며 절대 HTTP(S) URL이어야 한다. query/fragment는 허용하지 않고 path가 `/s/api/v{숫자}` 또는 `/s/v{숫자}`로 끝나야 한다. task path는 client가 아래처럼 조립하고 taskId를 URL encode한다. 입력 조회만 `APP_SERVER_INPUT_API_VERSION`(기본 `v2`)으로 부른다 — 기본 URL 끝의 버전만 바꾸고 result·callback은 기본 URL의 버전 그대로다(명세 2026-10-30). 입력 조회 v2의 `places`는 `{name, latitude, longitude}` 객체이고 AI는 이름을 `places`에, 좌표를 `placePoints`에 나눠 받는다(두 형태가 섞여도 받는다).

| Operation | Method/path | 전송 의미 |
|---|---|---|
| input | `GET /timeline/drafts/{taskId}/input` | source snapshot 조회 |
| result | `POST /timeline/drafts/{taskId}/result` | 확정 event 목록 저장 |
| callback | `POST /timeline/drafts/{taskId}/callback` | SUCCESS/FAILED 상태만 통보 |
| user-memory-result | `POST /user-memory/updates/{taskId}/result` | 갱신본 저장과 종료 통보를 겸함 (#64) |

앞의 셋은 Timeline 한 건의 순서 계약을 이룬다. 마지막 하나는 User Memory 갱신의 **유일한 outbound 호출**이라 지킬 순서가 없다.

모든 요청은 현재 `TaskToken` 값을 `Task-Token` header에 싣는다. 최초 token은 inbound body에서 받고, Timeline 경로에서는 성공 response body에 다른 비어 있지 않은 `taskToken`이 있으면 holder를 갱신한다. 실패 response의 token은 흡수하지 않는다. User Memory 갱신은 호출이 하나뿐이라 갱신될 기회 자체가 없다. token 값은 logging/tracing 대상이 아니며 `TaskToken.__str__`과 `repr`도 값을 가린다.

input response는 taskId, record date/timezone, optional window, 평평한 `sourceItems`와 optional 새 token을 제공한다. 이를 내부 `CollectedSnapshot`으로 변환한 뒤 다음 묶음 규칙을 검사한다.

- response taskId가 요청 taskId와 같음
- source item이 한 건 이상임
- 한 task 안에서 rawId가 유일함

접수 request window가 뒤에서 input response window를 덮어쓴다.

`userMemory`는 input 계약의 선택 필드다(#65). 응답에서는 원본 dict로 받고 `parse_user_memory()`가 `UserMemory` v1.0으로 따로 검증한다. 검증에 성공한 값만 `to_snapshot(user_memory=...)`으로 내부 snapshot에 들어간다. 필드가 없거나 `null`이면 `None`이다.

계약 위반(모르는 최상위 필드, 지원하지 않는 `schemaVersion`, 길이·개수 초과)은 묶음 규칙과 달리 **task를 실패시키지 않는다**. client가 code 1106으로 기록하고 memory 없이 진행한다. 응답 model에 `UserMemory`를 직접 선언하지 않는 이유가 이것이다 — 직접 선언하면 보조 context 하나가 응답 전체를 1102로 만든다.

result request는 내부 draft보다 좁다. App Server에는 event type, title, subtitle, place, address, 대표 좌표(`latitude`·`longitude`, #175), 장소 후보(`placeCandidates`, 최대 5개, 후보마다 입력 원소의 좌표), 시작·종료, source rawId 목록과 optional event question만 보낸다. 후보는 저장 직전에 그 event의 입력과 대조한다(`checked_place_candidates`) — 입력에 없는 이름·빈 이름·중복·5개 초과는 빼고, 좌표는 입력의 그 원소 값이다. `places` 밖에서 만든 후보(일정 장소명)는 좌표 키가 없다. 후보 좌표가 쌍·범위를 어기면 App Server가 batch 전체를 400으로 거절하므로 계약이 NaN·무한대·한쪽만을 받지 않는다. App Server는 1번 후보로 `place`와(좌표가 있으면) event 좌표를 덮어쓴다. confidence, inference level, uncertainty, 내부 warnings, tags, 입력의 장소 후보 목록(`places`), clientEventId는 보내지 않는다. 대표 좌표는 mapper가 최종 sourceRefs로 입력을 찾아 `place_resolver.representative_coordinates`로 채우며(이동은 마지막 도착 지점, 그 밖에는 address·place와 같은 지점, 없으면 가장 긴 체류), 둘 다 있거나 둘 다 없고 값이 없으면 키를 싣지 않는다 — 위치 근거 없는 event의 body는 필드를 더하기 전과 같다. App Server는 한쪽 좌표만 온 결과를 통째로 400으로 거절하므로 계약 모델이 짝을 강제한다. `placeCandidates`(`[{name}]`)는 계약에 있지만 v3 세트는 싣지 않는다 — App Server가 후보 1번으로 `place`를 덮어쓴다(App Server 공지 2026-10-29). v4 세트(#171)는 장소를 확정하지 못한 event에 `placeCandidates`(최대 5개, 1번이 `place`)를 싣고, `subtitle`의 장소 자리를 `{place}`로 보낸다. `{place}`가 남은 채 `place`·후보가 없으면 App Server가 결과 전체를 400으로 거절하므로 계약 모델이 그 조합을 막고, 결과 변환(`build_result_request`)이 그런 칸을 지명(구 + 동, 건물번호 없음)으로 바꾸거나 지운다. `place`/`address`는 `place_resolver`가 근거로 확정한 값이며 mapper는 검증하지 않고 옮기기만 한다 — 근거 없는 address는 확정 pass에서 이미 지워졌다. title/subtitle/place/address/question은 mapper에서 최대 255자로 방어하고, source rawId는 순서를 유지해 중복 제거하며, datetime은 draft timezone offset으로 보낸다. event가 0건이어도 결과 request를 전송한다.

callback body는 terminal status와 오류 필드뿐이다. taskId는 URL, token은 header에 있으므로 body에 반복하지 않는다. SUCCESS는 error가 모두 null이고 FAILED는 예약되지 않은 카탈로그 code와 정확한 안전 메시지 쌍이어야 한다.

user-memory-result request는 `status`와, `SUCCESS`면 전체 `userMemory` 갱신본, `FAILED`면 `errorCode`/`error`를 싣는다. 필드 짝은 model validator가 강제한다 — 성공에 갱신본이 없으면 App Server가 저장할 것이 없고, 실패에 code가 없으면 왜 안 바뀌었는지 알 수 없다. 실패에는 부분 결과를 싣지 않는다. `schemaVersion`과 `updatedAt`은 LLM 값이 아니라 서버가 박는다.

**이 호출에는 callback이 없다.** 성공도 실패도 이 한 번으로 통보하므로, runner의 모든 실패 경로가 여기로 수렴해야 한다. 빠뜨리면 실패를 알릴 수단이 하나도 없고 App Server 작업은 TTL까지 매달린다. 호출 자체가 retry까지 실패하면(1305) 통보할 다른 경로가 없다 — callback이 있어도 401/404/409에서는 같았으므로 회귀는 아니다.

`FAILED`는 "User Memory가 바뀌지 않았다"는 뜻이지 "하루 기록 저장이 실패했다"가 아니다. `DailyRecord`의 `DRAFT → SAVED` 전이는 앱 → App Server 구간에서 이미 끝나 있다. 둘을 한 transaction으로 묶으면 AI 실패가 사용자의 일기 저장을 되돌린다.

각 실제 request는 새 `httpx.AsyncClient`를 사용한다. singleton client가 서로 다른 event loop에 묶인 connection을 재사용하지 않기 위한 구현이다.

## Invariants

- App Server 접근 정책은 `app_server_client.py` 한곳에서 소유한다.
- token은 header 전용이고 값은 관측하지 않는다. 갱신 횟수만 허용한다.
- 2xx만 성공이다. 빈/non-JSON 2xx body는 허용하며 result 성공 body는 없어도 된다.
- timeout·transport error·5xx만 retry한다. 401/404/409는 abort, 다른 4xx는 callback 가능한 실패다.
- 저장 전 rawId 소속·필수값을 로컬 검증한다.
- result 성공 뒤에만 SUCCESS callback을 보낸다.
- User Memory 갱신은 어떤 경로로 끝나도 user-memory-result를 정확히 1회 호출한다.

## Known Gaps

- App Server 측 DB schema, transaction, idempotency key, task state machine은 이 저장소에 없어 검증할 수 없다.
- POST result/callback은 transport timeout 뒤 같은 body로 retry하지만 별도 idempotency header는 없다. 서버 측 중복 안전성은 이 저장소에서 확인할 수 없다.
- input response `userMemory`를 소비할 준비는 됐지만, App Server가 실제로 이 필드를 채워 보내는지는 이 저장소에서 확인할 수 없다.
- callback 실패를 영속적으로 재시도하는 queue가 없다.

## Update When

base URL 규칙, operation path/method, request·response shape, Task-Token 전달·갱신, retry/abort status, 결과 narrowing, 저장/callback 순서, User Memory 결과 통보 규칙이 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/services/test_app_server_client.py tests/services/test_timeline_runner.py tests/services/test_user_memory_runner.py -q`
- `uv run pytest tests/services/test_source_contract.py tests/services/test_timeline_result.py tests/services/test_timeline_validator.py -q`
- `uv run pytest tests/core/test_config.py -q`
- `rg -n "INPUT_PATH|RESULT_PATH|CALLBACK_PATH|USER_MEMORY_RESULT_PATH|TASK_TOKEN_HEADER|_ABORT_STATUSES" app/services/app_server_client.py`
