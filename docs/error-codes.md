# 오류 코드 계약

AI 서버가 밖으로 내보내는 실패는 **정수 하나로 식별**합니다. API 응답, App Server
완료 콜백, 운영 로그가 같은 실패에 같은 정수를 씁니다.

정본은 [`app/core/error_codes.py`](../app/core/error_codes.py)입니다. 이 문서는 그
카탈로그를 읽는 방법과 클라이언트 연동 방법을 설명합니다.

## 1. 응답 형식

```json
{
  "errorCode": 1201,
  "error": "타임라인 생성이 제한 시간을 초과했습니다."
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `errorCode` | `int` | 오류 종류를 식별하는 정수입니다. |
| `error` | `string` | 해당 오류를 설명하는 비어 있지 않은 문자열입니다. |

이 형식은 요청 검증 실패, `HTTPException`, 도메인 예외, 미처리 예외를 가리지 않고
동일합니다. 라우팅이 실패하는 404/405도 같은 형식으로 나갑니다.

## 2. 코드 대역

코드는 영역별 100단위 대역입니다. 코드만 봐도 어디서 깨졌는지 드러나고, 한 영역에
코드를 더해도 다른 영역과 충돌하지 않습니다.

| 대역 | 영역 |
|---|---|
| 1000~1099 | 요청/입력 계약 — 클라이언트가 보낸 것이 계약을 어긴 경우 |
| 1100~1199 | 원본 스냅샷 — App Server 입력 조회 API 응답/계약 |
| 1200~1299 | AI/LLM — 에이전트 실행, LLM 호출, 구조화 출력 |
| 1300~1399 | 결과 저장 — App Server 결과 저장 API, 저장 전 자체검증 |
| 1400~1499 | 외부 연동 — App Server 콜백, 인증/순서 거절 |
| 1900~1999 | 미분류 — 위 어디에도 속하지 않는 내부 오류 |

## 3. 코드 표

`HTTP` 열이 비어 있는 코드는 백그라운드 전용입니다. HTTP 응답으로는 나가지 않고,
완료 콜백과 운영 로그에만 나타납니다.

`흡수` 열이 ✓인 코드는 **처리를 중단시키지 않습니다**. 그 단계만 실패하고 파이프라인은
대체 결과로 계속 진행하며, 최종 상태는 `SUCCESS`일 수 있습니다. 추적을 위해 코드만
남습니다.

### 1000~1099 요청/입력 계약

| 코드 | 이름 | 의미 | HTTP | 흡수 |
|---|---|---|---|---|
| 1001 | `REQUEST_VALIDATION_FAILED` | 요청 본문이 계약을 어겼습니다. | 422 | |
| 1002 | `BAD_REQUEST` | 그 밖의 잘못된 요청입니다. | 400 | |
| 1003 | `NOT_FOUND` | 요청한 경로/리소스가 없습니다. | 404 | |
| 1004 | `METHOD_NOT_ALLOWED` | 허용되지 않은 HTTP 메서드입니다. | 405 | |
| 1008 | *(예약)* | 구 계약 `"ERROR_1008"`이 쓰던 번호입니다. **사용하지 않습니다.** | | |

### 1100~1199 원본 스냅샷

| 코드 | 이름 | 의미 | HTTP | 흡수 |
|---|---|---|---|---|
| 1101 | `SOURCE_SNAPSHOT_NOT_FOUND` | 입력 조회 API가 `taskId`의 수집 원본을 주지 않았습니다(404). | | |
| 1102 | `SOURCE_CONTRACT_VIOLATION` | 수집 원본 묶음이 입력 계약을 어겼습니다. | | |
| 1103 | `SOURCE_ITEM_NORMALIZE_FAILED` | 개별 수집 항목 정규화에 실패해 그 항목만 건너뛰었습니다. | | ✓ |
| 1104 | `TIMEZONE_RESOLUTION_FAILED` | 요청 timezone을 해석하지 못해 KST로 진행했습니다. | | ✓ |
| 1105 | `SOURCE_FETCH_FAILED` | 입력 조회 API 호출이 재시도까지 실패했습니다(5xx/timeout). | | |

### 1200~1299 AI/LLM

| 코드 | 이름 | 의미 | HTTP | 흡수 |
|---|---|---|---|---|
| 1201 | `PIPELINE_TIMEOUT` | 메인 에이전트가 제한 시간 안에 끝나지 않았습니다. | | |
| 1202 | `STRUCTURED_OUTPUT_INVALID` | 구조화 출력이 스키마 검증을 통과하지 못했습니다. | | |
| 1203 | `LLM_CALL_FAILED` | LLM provider 호출이 실패했습니다. | | |
| 1204 | `EVENT_AGENT_FAILED` | Event Agent가 실패해 그 결과 없이 진행했습니다. | | ✓ |
| 1205 | `TIMELINE_AGENT_FAILED` | Timeline Agent가 실패해 빈 draft로 진행했습니다. | | ✓ |
| 1206 | `REPAIR_AGENT_FAILED` | Repair Agent가 실패해 직전 정상 draft로 진행했습니다. | | ✓ |
| 1207 | `REPAIR_TOOL_FAILED` | Repair 도구 실행이 실패했습니다. | | ✓ |
| 1208 | `DRAFT_EDIT_FAILED` | draft 편집을 적용할 수 없습니다. | | ✓ |
| 1209 | `QUESTION_GENERATION_FAILED` | 회고 유도 질문 생성이 실패해 질문 없이 진행했습니다. | | ✓ |

1209는 질문 단계 하나의 흡수 코드입니다. 질문은 타임라인에 얹는 부가 가치이므로,
실패해도 event 본문은 그대로 저장되고 task는 SUCCESS로 끝납니다. 결과의 `question`이
전부 `null`인 것으로 나타납니다.

### 1300~1399 결과 저장

| 코드 | 이름 | 의미 | HTTP | 흡수 |
|---|---|---|---|---|
| 1301 | `TIMELINE_STORAGE_VALIDATION_FAILED` | 저장 전 자체검증에서 계약 위반이 나왔습니다. | | |
| 1302 | *(예약)* | AI 서버가 staging DB에 직접 붙던 시절의 `DATABASE_ERROR`입니다. **사용하지 않습니다.** | | |
| 1303 | `TIMELINE_RESULT_SUBMIT_FAILED` | 결과 저장 API 호출이 재시도까지 실패했습니다(5xx/timeout). | | |

### 1400~1499 외부 연동

| 코드 | 이름 | 의미 | HTTP | 흡수 |
|---|---|---|---|---|
| 1401 | `CALLBACK_SEND_FAILED` | App Server 완료 콜백 전송이 실패했습니다. | | ✓ |
| 1402 | *(예약)* | AI 서버가 관측 이벤트를 Elasticsearch로 직접 보내던 시절의 `OBSERVATION_EXPORT_FAILED`입니다. **사용하지 않습니다.** | | |
| 1403 | *(예약)* | 같은 시절의 `OBSERVATION_EMIT_FAILED`입니다. **사용하지 않습니다.** | | |
| 1404 | `APP_SERVER_UNAUTHORIZED` | App Server가 `taskToken`을 거절했습니다(401). | | |
| 1405 | `APP_SERVER_TASK_NOT_FOUND` | App Server에 task가 없거나 만료됐습니다(404). | | |
| 1406 | `APP_SERVER_CONFLICT` | App Server가 호출 순서 충돌로 거절했습니다(409). | | |
| 1407 | `PHOTO_IMAGE_FETCH_FAILED` | 사진 이미지 다운로드에 실패했습니다(HTTP 오류·timeout·형식/크기 거부·URL 정책 위반). | | ✓ |

1407은 사진 한 장 단위의 흡수 코드입니다. 해당 사진만 메타데이터 기반 설명으로
대체하고 타임라인 생성은 계속하므로, 이 코드만으로 task가 실패하지는 않습니다.
로그의 `reason` 필드로 원인을 구분합니다 — `host_not_allowed`(allowlist 밖),
`scheme_not_https`, `userinfo_present`, `allowlist_empty`(설정 누락),
`url_invalid`, `http_status`, `transport_error`(timeout·연결 실패), `unsupported_media_type`,
`too_large`, `empty_body`. `photoUrl` 값 자체는 어떤 로그에도 남지 않습니다.

### 1900~1999 미분류

| 코드 | 이름 | 의미 | HTTP | 흡수 |
|---|---|---|---|---|
| 1901 | `INTERNAL_ERROR` | 분류되지 않은 내부 오류입니다. | 500 | |

## 4. 클라이언트 연동

### 코드로 분기하고 문자열은 파싱하지 마세요

`error` 문구는 바뀔 수 있고 `errorCode`는 바뀌지 않습니다.

```java
switch (response.errorCode()) {
    case 1101 -> // 수집 원본이 없다. 원본 적재를 확인한다.
    case 1201 -> // 제한 시간 초과. 재시도 가능.
    default   -> // 그 밖은 총괄 실패로 처리한다.
}
```

### 모르는 코드는 총괄 실패로 처리하세요

AI 서버는 원인이 새로 분류될 때마다 코드를 추가합니다. 클라이언트가 모르는 정수를
받는 것은 **정상**이며, 그때는 대역(`code / 100`)으로 대략적인 원인을 잡거나 총괄
실패로 다루면 됩니다.

### 재시도 판단

| 코드 | 재시도 |
|---|---|
| 1201 `PIPELINE_TIMEOUT` | 가능 — 일시적 지연일 수 있습니다. |
| 1203 `LLM_CALL_FAILED` | 가능 — provider 일시 장애일 수 있습니다. |
| 1105 `SOURCE_FETCH_FAILED` | 가능 — App Server 일시 장애일 수 있습니다. |
| 1303 `TIMELINE_RESULT_SUBMIT_FAILED` | 가능 — 저장은 아직 되지 않았습니다. 같은 task로 다시 요청할 수 있습니다. |
| 1101 `SOURCE_SNAPSHOT_NOT_FOUND` | 불가 — 원본을 먼저 적재해야 합니다. |
| 1102 `SOURCE_CONTRACT_VIOLATION` | 불가 — 적재 데이터를 고쳐야 합니다. |
| 1001 `REQUEST_VALIDATION_FAILED` | 불가 — 요청을 고쳐야 합니다. |
| 1404 `APP_SERVER_UNAUTHORIZED` | 불가 — 토큰이 무효합니다. AI 서버는 콜백 없이 중단합니다. |
| 1405 `APP_SERVER_TASK_NOT_FOUND` | 불가 — task가 만료됐거나 없습니다. AI 서버는 콜백 없이 중단합니다. |
| 1406 `APP_SERVER_CONFLICT` | 불가 — 호출 순서가 어긋났습니다. AI 서버는 콜백 없이 중단합니다. |

### 내부 정보는 응답에 없습니다

`error`에는 카탈로그의 고정 문장만 담깁니다. Agent 실패를 흡수해 최종 draft에
남기는 warning도 같은 안전 메시지를 사용합니다. 원본 예외 메시지, `taskId`/`rawId`
같은 식별자, 경로, traceback은 **서버 로그에만** 남습니다. 장애를 추적할 때는
`errorCode`로 로그를 조회하세요.

## 5. 로그에서 찾기

실패는 두 자리에 남고, 목적이 다릅니다(이슈 #53).

**Elasticsearch — 운영 이벤트.** 최종 경계가 `errorCode`를 **구조화 필드**로 담아
남깁니다. HTTP 요청은 `http.request.completed`, 백그라운드 작업은
`timeline.task.completed`, 외부 연동은 `dependency.request.completed` 입니다.

```json
{"timestamp":"2026-08-01T04:12:07.882Z","log.level":"ERROR",
 "logger":"app.operational","message":"Timeline 작업 완료",
 "service":"laimory-ai","environment":"prod",
 "event.dataset":"laimory.api","event.action":"timeline.task.completed",
 "event.outcome":"failure","taskId":"task-1","status":"FAILED",
 "errorCode":1301,"failureStage":"STORAGE","durationMs":41822.5,"callbackSent":true}
```

값이 메시지 문자열이 아니라 필드에 있으므로 Kibana에서 그대로 필터·집계할 수 있습니다.

```
errorCode: 1301 and environment: "prod"
```

**컨테이너 stdout — 로컬 진단.** `report_error`가 남기는 줄에는 `errorType`,
`errorMessage`(마스킹한 원본), 최종 실패의 traceback까지 있습니다. 이 줄은 수집
표식이 없어 Elasticsearch로 가지 않으므로 `docker logs laimory-ai`로 봅니다.

```json
{"log.level":"WARNING","logger":"app.services.timeline_runner",
 "message":"타임라인 처리 실패","taskId":"task-1","stage":"STORAGE",
 "errorCode":1301,"errorType":"TimelineValidationError",
 "violationCodes":["SOURCE_RAW_ID_NOT_IN_TASK"],"violationCount":1}
```

즉 **무엇이 얼마나 실패하는지는 Elasticsearch, 왜 실패했는지의 원문은 컨테이너
로그, AI 실행 과정은 Langfuse**입니다. 세 곳을 잇는 상관키는 `taskId` 하나입니다.

조회 예시와 필드 계약은 [docs/operational-logging.md](operational-logging.md)에
정리돼 있습니다.

## 6. 새 코드를 추가할 때

1. 해당 영역 대역에서 **비어 있는 가장 작은 번호**를 씁니다. 번호는 재사용하지
   않습니다 — 한번 나간 코드는 클라이언트 분기와 과거 로그에 남아 있습니다.
2. `ErrorCode`에 멤버를 추가하고 `_MESSAGES`에 **외부로 보여도 안전한** 문장을
   넣습니다. 경로·쿼리·식별자·예외 클래스명·스택을 넣지 않습니다.
3. HTTP 응답으로 나갈 수 있는 코드만 `_HTTP_STATUSES`에 넣습니다. 백그라운드 전용
   코드는 넣지 않으며, 조회 시 500으로 떨어집니다.
4. 이 문서의 코드 표에 행을 추가합니다.
5. 대역이 꽉 차면 새 대역을 열고 `error_codes.py` docstring과 이 문서의 대역 표에
   함께 추가합니다.

값이 중복되면 `_assert_unique_values()`가 **import 시점에** 실패해 서버가 뜨지
않습니다. 메시지가 비어 있어도 마찬가지입니다. 운영에 나간 뒤 발견하는 것보다 낫습니다.

## 7. 코드를 남기는 방법

`except` 블록은 [`report_error`](../app/core/exceptions.py)만 호출합니다. 이 함수가
실패에 **코드를 부여하고** 로컬 진단으로 남기는 통로입니다. 각자 로그를 찍으면
언젠가 값이 갈리는데, 그때 갈렸다는 사실조차 알기 어렵습니다.

돌려주는 코드는 호출부가 응답·콜백·운영 이벤트에 실어야 세 곳이 같은 값을 말합니다.
Elasticsearch로 나가는 것은 최종 경계의 운영 이벤트뿐입니다(이슈 #53) — 그래서
`report_error`는 **다시 던지는 중간 경계가 아니라 최종 경계나 흡수 지점**에서
부릅니다. LLM → Agent → 노드 → runner 처럼 올라오는 길목마다 부르면 실패 하나가
여러 건으로 집계됩니다.

```python
except Exception as exc:
    failure_code = report_error(
        logger,
        code_of(exc),              # 예외가 아는 코드. 모르면 INTERNAL_ERROR.
        "타임라인 처리 실패",        # 무슨 실패인지 한 줄
        exc=exc,
        context={"taskId": task_id},   # 로그에만 나가는 구조화 진단 필드
        stage=ExecutionStage.STORAGE,
        exc_info=True,
    )
```

`taskId`·`stage`·`agent`는 실행 컨텍스트([`app/core/execution_context.py`](../app/core/execution_context.py))가
자동으로 붙이므로, 컨텍스트와 다른 값을 남길 때만 직접 넘기면 됩니다.

도메인 예외는 `AppError`를 상속해 자기 코드를 갖습니다. 그러면 잡는 쪽이 예외
클래스명을 알아보지 않고도 `code_of(exc)`로 코드를 꺼낼 수 있습니다.

```python
class SourceBatchError(AppError, ValueError):
    default_code = ErrorCode.SOURCE_CONTRACT_VIOLATION
```

### 본문을 로그에 담지 않습니다

`context`에 넣는 값은 식별자·건수·상태 같은 진단 지표입니다. 프롬프트, LLM 응답,
draft 전문, 사용자 원문, LLM이 만든 도구 인자 **값**은 넣지 않습니다. 그 수준의
추적은 Langfuse가 담당합니다.

운영 이벤트에는 `context`가 그대로 실리지 않습니다. 이벤트마다 허용 필드가 정해져
있고([`app/core/operational_logging.py`](../app/core/operational_logging.py)),
목록에 없는 이름은 버려집니다. 그래서 호출부 실수 하나로 사용자 데이터가
Elasticsearch까지 가지는 않습니다 — 다만 로컬 로그에는 남으므로 여전히 조심합니다.

### 코드를 붙이지 않는 곳

형식을 하나씩 시도해 보는 **탐색용** `except`는 오류 경로가 아니라 제어 흐름입니다
(`validator.parse_datetime`이 날짜 포맷을 차례로 시도하는 부분 등). 여기에 로그를
남기면 정상 동작이 매번 경고를 뿜습니다.

예외를 삼키지 않고 그대로 다시 던지는 곳(`app_server_client`의 재시도 소진 전
중간 시도 등)도 따로 남기지 않습니다. 상위에서 같은 예외를 코드와 함께 기록하므로
중복입니다.
