# Laimory AI 서버 API 명세

> 기준일: 2026-07-22  
> 현재 구현 기준

## 1. 처리 구조

AI 서버는 타임라인 생성 요청을 비동기로 처리합니다.

```text
App Server
→ 원본 데이터를 timeline_draft_source_items에 저장
→ AI 서버에 타임라인 생성 요청
→ AI 서버가 202 Accepted 반환
→ AI 서버가 타임라인 생성 및 DB 저장
→ AI 서버가 App Server에 SUCCESS 또는 FAILED 콜백
→ App Server가 DB에서 결과 조회
```

AI 서버는 작업 상태를 별도로 저장하지 않으며, 작업 상태 조회용 GET API도 제공하지 않습니다.

## 2. 공통 정보

### Base URL

```text
http://{AI_SERVER_HOST}:8000
```

로컬 환경:

```text
http://127.0.0.1:8000
```

AgentCore Runtime에 배포한 컨테이너는 `8080` 포트를 사용하며, App Server는 HTTP로 직접 호출하지 않고 `InvokeAgentRuntime`으로 호출합니다. 자세한 내용은 [AgentCore Runtime 배포 가이드](deploy-agentcore.md)를 참고합니다.

### Content-Type

```http
Content-Type: application/json
```

### 날짜 형식

날짜와 시간은 타임존을 포함한 ISO 8601 형식을 사용합니다.

```text
2026-07-22T09:00:00+09:00
```

### 인증

현재 별도의 API 인증 방식은 구현되어 있지 않습니다.

## 3. API 목록

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/v1/timeline` | 타임라인 생성 작업 접수 |
| `GET` | `/health` | AI 서버 상태 확인 |
| `POST` | `/invocations` | AgentCore Runtime 호출 진입점. `/v1/timeline`과 동일하게 처리 |
| `GET` | `/ping` | AgentCore Runtime 헬스체크 |

`/invocations`와 `/ping`은 AgentCore Runtime이 컨테이너에 요구하는 고정 경로입니다.

## 4. 타임라인 생성 요청

### `POST /v1/timeline`

타임라인 생성 작업을 접수합니다. 실제 처리는 백그라운드에서 진행됩니다.

`202 Accepted`는 타임라인 생성 완료가 아니라 요청 접수 완료를 의미합니다.

AgentCore Runtime에 배포한 환경에서는 `POST /invocations`가 동일한 요청 본문을 받아 같은 방식으로 처리합니다. 요청·응답 형식이 같으므로 아래 명세를 그대로 사용합니다.

### 사전 조건

- `timeline_draft_source_items`에 동일한 `taskId`를 가진 원본 데이터가 존재해야 합니다.
- `dailyRecordId`에 해당하는 `daily_records` 데이터가 먼저 생성되어 있어야 합니다.
- 동일 작업에 포함된 원본 데이터는 한 사용자의 데이터여야 합니다.
- 동일 작업 안에서 원본 데이터의 `raw_id`는 중복될 수 없습니다.

### Request Body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---:|---|
| `taskId` | `string` | O | 작업 식별자. `timeline_draft_source_items.task_id`와 일치해야 합니다. |
| `callbackToken` | `string` | O | 완료 콜백 인증 토큰입니다. 콜백의 `Callback-Token` 헤더로 반환됩니다. |
| `dailyRecordId` | `integer` | O | 생성된 타임라인 이벤트를 연결할 Daily Record ID입니다. |
| `window` | `object` | O | 타임라인 생성 범위입니다. |
| `window.startAt` | `datetime` | O | 생성 범위 시작 시각입니다. |
| `window.endAt` | `datetime` | O | 생성 범위 종료 시각입니다. |

### Request Example

```json
{
  "taskId": "task-20260722-001",
  "callbackToken": "callback-token-001",
  "dailyRecordId": 42,
  "window": {
    "startAt": "2026-07-22T00:00:00+09:00",
    "endAt": "2026-07-23T00:00:00+09:00"
  }
}
```

### Success Response

#### `202 Accepted`

```json
{
  "taskId": "task-20260722-001",
  "status": "PROCESSING"
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `taskId` | `string` | 접수된 작업 ID입니다. |
| `status` | `string` | 접수 응답에서는 항상 `PROCESSING`입니다. |

### Error Response

오류 응답은 경로와 상태 코드를 가리지 않고 항상 같은 형식입니다.

```json
{
  "errorCode": 1001,
  "error": "요청 형식이 올바르지 않습니다."
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `errorCode` | `int` | 오류 종류를 식별하는 정수입니다. |
| `error` | `string` | 해당 오류를 설명하는 비어 있지 않은 문자열입니다. |

| 상태 | `errorCode` | 발생 조건 |
|---|---|---|
| `422 Unprocessable Entity` | `1001` | 필수 필드가 없거나 날짜 형식이 올바르지 않습니다. |
| `404 Not Found` | `1003` | 없는 경로입니다. |
| `405 Method Not Allowed` | `1004` | 허용되지 않은 HTTP 메서드입니다. |
| `500 Internal Server Error` | `1901` | 분류되지 않은 서버 내부 오류입니다. |

`error`에는 사전에 정의된 안전한 메시지만 담깁니다. 어느 필드가 왜 틀렸는지, 어떤
값을 보냈는지는 응답에 포함되지 않고 AI 서버 로그에만 남습니다. 전체 코드 표는
[docs/error-codes.md](error-codes.md)를 참고하세요.

> **계약 변경 (이슈 #42)**
> 이전에는 FastAPI 기본 검증 오류가 `{"detail": [...]}` 형태로 위반 필드와 입력값을
> 그대로 반환했습니다. 지금은 위 공통 형식으로 통일됐습니다.

### 처리 실패

요청 접수 이후 발생한 오류는 이미 반환된 `202` 응답에 반영되지 않습니다. 최종 성공 또는 실패 여부는 완료 콜백으로 전달됩니다.

## 5. 완료 콜백

AI 서버는 처리가 끝나면 서버에 설정된 `APP_SERVER_API_URL`에 task별 콜백
경로를 붙여 결과 상태를 전송합니다.

```env
APP_SERVER_API_URL=https://api.example.com/s/api/v1
```

### Request

```http
POST {APP_SERVER_API_URL}/timeline/drafts/{taskId}/callback
Callback-Token: {callbackToken}
Content-Type: application/json
```

`taskId`는 URL path에 안전하게 인코딩하며, `callbackToken`은 body나 로그에
포함하지 않고 인증 헤더로만 전달합니다.

### 성공 콜백

타임라인 생성과 DB 저장이 모두 완료된 경우입니다.

```json
{
  "status": "SUCCESS",
  "errorCode": null,
  "error": null
}
```

### 실패 콜백

원본 조회, AI 처리, 검증 또는 DB 저장 중 오류가 발생한 경우입니다.

```json
{
  "status": "FAILED",
  "errorCode": 1201,
  "error": "타임라인 생성이 제한 시간을 초과했습니다."
}
```

### Callback Body

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | `string` | 최종 상태인 `SUCCESS` 또는 `FAILED`입니다. |
| `errorCode` | `int \| null` | 실패 원인을 식별하는 정수입니다. 성공은 `null`입니다. |
| `error` | `string \| null` | 실패를 설명하는 비어 있지 않은 문자열입니다. 성공은 `null`입니다. |

실패 원인별 `errorCode`는 다음과 같습니다. 전체 표와 재시도 판단 기준은
[docs/error-codes.md](error-codes.md)에 있습니다.

| 실패 원인 | `errorCode` |
|---|---|
| 수집 원본을 찾지 못함 | `1101` |
| 수집 원본이 입력 계약 위반 | `1102` |
| 메인 에이전트 제한 시간 초과 | `1201` |
| AI 응답 스키마 검증 실패 | `1202` |
| 저장 전 자체검증 실패 | `1301` |
| DB 접근/트랜잭션 실패 | `1302` |
| 분류되지 않은 내부 오류 | `1901` |

> **계약 변경 (이슈 #42)**
> 이전에는 모든 실패가 문자열 `"ERROR_1008"` 하나로 전달됐고 `error`에는 원본 예외
> 메시지가 그대로 실렸습니다. 지금은 `errorCode`가 **정수**이며 원인별로 다릅니다.
> `error`에는 사전에 정의된 안전한 메시지만 나가고, 원본 예외 메시지는 AI 서버
> 로그에만 남습니다.
>
> App Server는 `errorCode`를 정수로 읽어야 하며, **모르는 코드는 총괄 실패로**
> 처리하면 됩니다. 원인이 새로 분류될 때마다 코드가 추가됩니다.

### App Server 응답

- App Server의 정상 응답은 HTTP `200 OK`이며, AI 서버는 HTTP `2xx`를 콜백
  전송 성공으로 처리합니다.
- 응답 Body는 사용하지 않습니다.

### 유의사항

- 콜백에는 생성된 타임라인 데이터가 포함되지 않습니다.
- App Server는 `SUCCESS` 콜백을 받은 후 DB에서 결과를 조회합니다.
- 현재 콜백 재시도는 구현되어 있지 않습니다.
- 콜백 전송 실패가 이미 저장된 타임라인 데이터를 되돌리지는 않습니다.

## 6. DB 입출력

### 입력 테이블

| 테이블 | 설명 |
|---|---|
| `timeline_draft_source_items` | AI 서버가 `taskId`로 원본 데이터를 조회합니다. AI 서버는 이 테이블을 수정하지 않습니다. |

### 출력 테이블

| 테이블 | 설명 |
|---|---|
| `timeline_events` | 생성된 최종 타임라인 이벤트를 저장합니다. |
| `timeline_items` | 이벤트 생성에 사용한 원본 항목을 `raw_id` 기준으로 저장합니다. |
| `timeline_event_items` | 이벤트와 원본 항목의 N:M 관계를 저장합니다. |

`timeline_draft_event_suggestions`는 현재 처리 흐름에서 사용하지 않습니다.

### 저장 규칙

- API로 받은 `dailyRecordId`를 `timeline_events.daily_record_id`에 연결합니다.
- 이벤트의 `sourceRefs.rawId`를 이용해 원본 항목과 이벤트의 관계를 저장합니다.
- 하나의 이벤트는 여러 원본 항목과 연결될 수 있습니다.
- 하나의 원본 항목은 여러 이벤트와 연결될 수 있습니다.
- 요청한 `taskId`에 속하지 않는 `rawId`가 결과에 포함되면 저장에 실패합니다.
- 같은 `dailyRecordId`를 다시 처리하면 기존 AI 생성 데이터만 교체합니다.
- 사용자가 생성한 데이터는 교체 대상에 포함하지 않습니다.

## 7. 서버 상태 확인

### `GET /health`

AI 서버 프로세스가 실행 중인지 확인합니다.

#### `200 OK`

```json
{
  "status": "ok"
}
```

이 API는 DB, LLM Provider 또는 콜백 서버의 연결 상태까지 확인하지는 않습니다.

### `GET /ping`

AgentCore Runtime이 컨테이너를 계속 살려둘지 판단하기 위해 호출하는 헬스체크입니다.

#### `200 OK`

```json
{
  "status": "Healthy"
}
```

| 값 | 의미 |
|---|---|
| `Healthy` | 진행 중인 백그라운드 처리가 없습니다. |
| `HealthyBusy` | 접수한 작업을 아직 처리하고 있습니다. |

AI 서버는 요청을 `202`로 접수한 뒤 백그라운드에서 처리를 이어갑니다. 응답을 이미 반환한 뒤에도 처리가 남아 있으면 `HealthyBusy`를 반환해 컨테이너가 회수되지 않도록 합니다.

이 상태값은 작업 상태가 아닙니다. `taskId`를 담지 않으며 특정 작업의 진행 여부를 조회하는 용도로 사용할 수 없습니다. 작업 상태는 App Server가 소유하고 AI 서버는 콜백으로만 통보합니다.

## 8. 상태값

| 값 | 사용 위치 | 설명 |
|---|---|---|
| `PROCESSING` | 타임라인 생성 접수 응답 | 요청이 접수되어 백그라운드 처리를 시작함 |
| `SUCCESS` | 완료 콜백 | 타임라인 생성과 DB 저장이 완료됨 |
| `FAILED` | 완료 콜백 | 원본 조회, AI 처리, 검증 또는 DB 저장에 실패함 |

## 9. API 문서 확인

서버 실행 후 다음 경로에서 자동 생성된 문서를 확인할 수 있습니다.

| 문서 | 경로 |
|---|---|
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI JSON | `/openapi.json` |
