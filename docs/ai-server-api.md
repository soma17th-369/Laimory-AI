# Laimory AI 서버 API 명세

> 기준일: 2026-07-31
> 현재 구현 기준

## 1. 처리 구조

AI 서버는 타임라인 생성 요청을 비동기로 처리합니다. 데이터 접근은 **App Server
서버간 API 하나로 일원화**되어 있습니다(이슈 #40). AI 서버는 데이터베이스에 직접
접근하지 않습니다.

```text
App Server
→ AI 서버에 타임라인 생성 요청 (taskId, taskToken)
→ AI 서버가 202 Accepted 반환
→ AI 서버가 입력 조회 API 호출   GET  /timeline/drafts/{taskId}/input
→ AI 서버가 타임라인 생성(추론)
→ AI 서버가 결과 저장 API 호출   POST /timeline/drafts/{taskId}/result
→ 저장 200 확인 후 완료 콜백     POST /timeline/drafts/{taskId}/callback
```

AI 서버는 작업 상태를 별도로 저장하지 않으며, 작업 상태 조회용 GET API도 제공하지 않습니다.

### 호출 순서 계약

- 입력 조회 → 추론 → 결과 저장 → 콜백 순서를 지킵니다.
- **결과 저장 200을 확인한 뒤에만** `SUCCESS` 콜백을 보냅니다.
- 결과 저장에 성공한 뒤에는 어떤 이유로도 `FAILED`를 보내지 않습니다.

### taskToken

작업 하나에 토큰 하나를 사용합니다.

| 구분 | 위치 |
|---|---|
| 최초 발급 | 타임라인 생성 요청 body의 `taskToken` |
| 이후 갱신 | App Server 응답 body의 `taskToken` |
| 인증 | 모든 AI → App Server 요청의 `Task-Token` 헤더 |

- 응답 body에 `taskToken`이 있으면 그 값으로 갱신하고, 이후 요청은 갱신된 값을 씁니다.
- 응답에 토큰이 없으면 직전 값을 계속 사용합니다.
- AI 서버는 토큰을 파생하거나 변형하지 않으며, 로그·관측 데이터에 기록하지 않습니다.
- 성공(2xx) 응답의 토큰만 받아들입니다. 거절된 응답 body의 값은 무시합니다.

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

- 입력 조회 API가 동일한 `taskId`의 원본 데이터를 반환할 수 있어야 합니다.
- `taskToken`이 세 API(입력 조회·결과 저장·콜백) 모두에서 유효해야 합니다.
- 동일 작업 안에서 원본 데이터의 `rawId`는 중복될 수 없습니다.

### Request Body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---:|---|
| `taskId` | `string` | O | 작업 식별자입니다. |
| `taskToken` | `string` | O | 이 작업의 최초 토큰입니다. AI 서버가 App Server를 호출할 때 `Task-Token` 헤더로 사용합니다. |
| `dailyRecordId` | `integer` | O | 생성된 타임라인 이벤트를 연결할 Daily Record ID입니다. |
| `window` | `object` | O | 타임라인 생성 범위입니다. |
| `window.startAt` | `datetime` | O | 생성 범위 시작 시각입니다. |
| `window.endAt` | `datetime` | O | 생성 범위 종료 시각입니다. |

### Request Example

```json
{
  "taskId": "task-20260722-001",
  "taskToken": "task-token-001",
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

## 5. AI 서버가 호출하는 App Server API

AI 서버는 설정된 `APP_SERVER_API_URL`에 task별 경로를 붙여 세 개의 API를 호출합니다.

```env
APP_SERVER_API_URL=https://api.example.com/s/api/v1
```

| 순서 | Method | 경로 |
|---|---|---|
| 1 | `GET` | `{APP_SERVER_API_URL}/timeline/drafts/{taskId}/input` |
| 2 | `POST` | `{APP_SERVER_API_URL}/timeline/drafts/{taskId}/result` |
| 3 | `POST` | `{APP_SERVER_API_URL}/timeline/drafts/{taskId}/callback` |

`APP_SERVER_API_URL`은 **필수 설정**입니다. AI 서버의 유일한 데이터 경로이므로 값이
없으면 서버가 기동하지 않습니다. 버전 경로까지 넣으며 `/s/api/v1`과 `/s/v1` 두 형태를
모두 허용합니다(환경에 따라 접두사가 다릅니다). 버전 경로가 빠진 값은 거부합니다.

### 공통 규칙

```http
Task-Token: {taskToken}
Content-Type: application/json
```

- `taskId`는 URL path에 안전하게 인코딩합니다.
- `taskToken`은 `Task-Token` 헤더로만 전달합니다. URL, 요청 body, 로그에 넣지 않습니다.
- 응답 body에 `taskToken`이 오면 그 값으로 갱신해 이후 요청에 사용합니다.

### 재시도와 중단

| 응답 | AI 서버 동작 |
|---|---|
| `2xx` | 다음 단계로 진행합니다. |
| timeout, `5xx` | **같은 토큰과 같은 body로** 재시도합니다(기본 3회, 0.5초부터 2배씩 대기). |
| `401` | 토큰 오류입니다. 재시도하지 않고 중단하며 콜백도 보내지 않습니다. |
| `404` | task가 없거나 만료됐습니다. 재시도하지 않고 중단하며 콜백도 보내지 않습니다. |
| `409` | 호출 순서 충돌입니다. 재시도하지 않고 중단하며 콜백도 보내지 않습니다. |
| 그 밖의 `4xx` | 재시도하지 않고 실패로 처리한 뒤 `FAILED` 콜백을 보냅니다. |

401/404/409에서 콜백을 보내지 않는 이유는 콜백도 같은 이유로 거절되기 때문입니다.

### 5.1 입력 조회

```http
GET {APP_SERVER_API_URL}/timeline/drafts/{taskId}/input
Task-Token: {taskToken}
```

#### Response `200 OK`

```json
{
  "taskId": "task-20260722-001",
  "recordDate": "2026-07-22",
  "recordTimeZone": "Asia/Seoul",
  "window": {
    "startAt": "2026-07-22T00:00:00+09:00",
    "endAt": "2026-07-23T00:00:00+09:00"
  },
  "sourceItems": [
    {
      "rawId": "b1f0b6d5-5c3e-4a4e-9a37-2f1f0d2f3b71",
      "itemType": "PHOTO",
      "startAt": "2026-07-22T12:10:00+09:00",
      "endAt": null,
      "payload": {
        "photoUrl": "https://<버킷>.s3.<리전>.amazonaws.com/<키>?<서명>"
      }
    }
  ]
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `taskId` | `string` | 요청한 작업 ID와 같아야 합니다. |
| `taskToken` | `string \| null` | 갱신 토큰입니다. 있으면 이후 요청에 사용합니다. |
| `recordDate` | `string` | 대상 날짜입니다. |
| `recordTimeZone` | `string` | 시간대입니다. 기본값은 `Asia/Seoul`입니다. |
| `window` | `object` | 수집 범위입니다. 실제 생성 범위는 접수 요청의 `window`가 정본입니다. |
| `sourceItems[].rawId` | `string` | 원본 식별자(UUID)입니다. 결과의 `sourceRawIds`가 참조합니다. |
| `sourceItems[].itemType` | `string` | `PHOTO`, `CALENDAR`, `STAY`, `MOVEMENT`, `HEALTH`, `NOTIFICATION` |
| `sourceItems[].startAt` | `datetime` | 항목 시각입니다. |
| `sourceItems[].endAt` | `datetime \| null` | 종료 시각입니다. |
| `sourceItems[].payload` | `object` | `itemType`별 원본 데이터입니다. |

`itemType=PHOTO`의 `payload.photoUrl`은 S3 이미지 URL입니다. AI 서버가 이 URL에서
이미지를 내려받아 멀티모달 LLM으로 사진 설명을 만듭니다(이슈 #52, #59).

- 호스트 allowlist는 없습니다. `http`/`https` URL이면 호스트와 무관하게 내려받되,
  redirect는 따라가지 않습니다(이슈 #59).
- 형식은 JPEG·PNG·WebP, 장당 5MB, 한 번에 최대 20장까지 받습니다. 한 배치의 이미지
  총합은 20MB로 제한합니다.
- presigned URL이면 **입력 조회부터 이미지 다운로드까지가 유효 시간 안에** 끝나야
  합니다. 만료된 URL은 다운로드 실패로 처리되며, 해당 사진만 메타데이터 기반 설명으로
  대체하고 타임라인 생성은 계속합니다(오류 코드 `1407`, 흡수).
- `photoUrl` 값은 운영 로그·Langfuse·LLM 프롬프트 어디에도 남기지 않습니다.

다음 조건은 입력 계약 위반(`1102`)으로 처리합니다.

- 응답 `taskId`가 요청 `taskId`와 다름
- `sourceItems`가 비어 있음
- 같은 `rawId`가 두 번 이상 나옴
- `rawId`가 UUID가 아니거나 `startAt`이 없음

### 5.2 결과 저장

```http
POST {APP_SERVER_API_URL}/timeline/drafts/{taskId}/result
Task-Token: {taskToken}
```

```json
{
  "events": [
    {
      "eventType": "MEAL",
      "title": "점심",
      "subtitle": null,
      "startAt": "2026-07-22T12:00:00+09:00",
      "endAt": "2026-07-22T13:00:00+09:00",
      "sourceRawIds": ["b1f0b6d5-5c3e-4a4e-9a37-2f1f0d2f3b71"]
    }
  ]
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `eventType` | `string` | 아래 13종 중 하나입니다. |
| `title` | `string` | 이벤트 제목입니다. 255자로 자릅니다. |
| `subtitle` | `string \| null` | 부제입니다. 내용이 없으면 `null`입니다. 255자로 자릅니다. |
| `startAt` | `datetime` | 시작 시각입니다. `recordTimeZone` 기준 offset으로 보냅니다. |
| `endAt` | `datetime` | 종료 시각입니다. `startAt` 이상입니다. |
| `sourceRawIds` | `string[]` | 근거 원본의 `rawId`입니다. 1개 이상이며 중복은 제거합니다. |

`eventType` 값:

```text
WAKE_UP, SLEEP, MOVEMENT, CALENDAR_EVENT, MEAL, PHOTO_MOMENT,
MEETING, CLASS, WORK, EXERCISE, SOCIAL, REST, UNKNOWN
```

- 성공 응답은 `200 OK`이며 body는 사용하지 않습니다.
- 입력에 없는 `rawId`가 결과에 남아 있으면 AI 서버가 저장 요청 자체를 보내지 않고
  `1301`로 실패 처리합니다.
- 이벤트가 0건이어도 요청을 보냅니다. "생성 결과 없음"도 확정된 결과입니다.

### 5.3 완료 콜백

```http
POST {APP_SERVER_API_URL}/timeline/drafts/{taskId}/callback
Task-Token: {taskToken}
```

#### 성공 콜백

결과 저장 `200`을 확인한 뒤에만 보냅니다.

```json
{
  "status": "SUCCESS",
  "errorCode": null,
  "error": null
}
```

#### 실패 콜백

입력 조회, 추론, 저장 전 검증, 결과 저장 중 실패한 경우입니다.

```json
{
  "status": "FAILED",
  "errorCode": 1201,
  "error": "타임라인 생성이 제한 시간을 초과했습니다."
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | `string` | 최종 상태인 `SUCCESS` 또는 `FAILED`입니다. |
| `errorCode` | `int \| null` | 실패 원인을 식별하는 정수입니다. 성공은 `null`입니다. |
| `error` | `string \| null` | 실패를 설명하는 비어 있지 않은 문자열입니다. 성공은 `null`입니다. |

실패 원인별 `errorCode`는 다음과 같습니다. 전체 표와 재시도 판단 기준은
[docs/error-codes.md](error-codes.md)에 있습니다.

| 실패 원인 | `errorCode` |
|---|---|
| 입력 조회가 원본을 반환하지 않음(404) | `1101` |
| 입력이 계약 위반 | `1102` |
| 입력 조회 호출 실패(5xx/timeout 소진) | `1105` |
| 메인 에이전트 제한 시간 초과 | `1201` |
| AI 응답 스키마 검증 실패 | `1202` |
| 저장 전 자체검증 실패 | `1301` |
| 결과 저장 호출 실패(5xx/timeout 소진) | `1303` |
| 분류되지 않은 내부 오류 | `1901` |

> **계약 변경 (이슈 #40)**
> `Callback-Token` 헤더가 `Task-Token`으로 바뀌었고, 토큰 이름도 `callbackToken`에서
> `taskToken`으로 통일됐습니다. 콜백 body는 그대로입니다.
>
> `errorCode`는 이슈 #42에서 정한 **원인별 정수**를 계속 사용합니다. App Server는
> 모르는 코드를 총괄 실패로 처리하면 됩니다.

### App Server 응답

- 정상 응답은 HTTP `200 OK`이며, AI 서버는 HTTP `2xx`를 성공으로 처리합니다.
- 콜백 응답 body는 사용하지 않습니다(`taskToken` 갱신은 예외).

### 유의사항

- 콜백에는 생성된 타임라인 데이터가 포함되지 않습니다. 결과는 결과 저장 API로 이미
  전달됐습니다.
- 콜백 전송 실패는 이미 저장된 결과를 되돌리지 않습니다.
- 콜백 timeout/5xx는 같은 토큰과 같은 body로 재시도합니다.

## 6. 데이터 접근 경계

| 대상 | 소유 | AI 서버 접근 |
|---|---|---|
| 수집 원본 | App Server | 입력 조회 API (읽기) |
| 타임라인 결과 | App Server | 결과 저장 API (쓰기) |
| 작업 상태 | App Server | 콜백으로 통보만 |

AI 서버는 데이터베이스에 직접 접근하지 않으며, 운영에 DB 접속 정보와 네트워크
권한이 필요하지 않습니다.

> **계약 변경 (이슈 #40)**
> 이전에는 AI 서버가 staging MySQL의 `timeline_draft_source_items`를 직접 조회하고
> `timeline_events`/`timeline_items`/`timeline_event_items`에 직접 저장했습니다.
> 지금은 두 경로 모두 App Server API로 대체됐습니다. `dailyRecordId`는 접수 요청에
> 그대로 남아 있지만 저장 연결은 App Server가 담당합니다.

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
| `SUCCESS` | 완료 콜백 | 타임라인 생성과 결과 저장이 완료됨 |
| `FAILED` | 완료 콜백 | 입력 조회, AI 처리, 검증 또는 결과 저장에 실패함 |

## 9. API 문서 확인

서버 실행 후 다음 경로에서 자동 생성된 문서를 확인할 수 있습니다.

| 문서 | 경로 |
|---|---|
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI JSON | `/openapi.json` |
