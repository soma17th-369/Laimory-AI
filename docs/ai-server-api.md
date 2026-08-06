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
| `POST` | `/v1/user-memory` | User Memory 갱신 작업 접수 |
| `GET` | `/health` | AI 서버 상태 확인 |
| `POST` | `/invocations` | AgentCore Runtime 호출 진입점. `/v1/timeline`과 동일하게 처리 |
| `GET` | `/ping` | AgentCore Runtime 헬스체크 |

`/invocations`와 `/ping`은 AgentCore Runtime이 컨테이너에 요구하는 고정 경로입니다.
`/invocations`는 타임라인 전용이며 User Memory 갱신을 받지 않습니다.

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

## 4-2. User Memory 갱신 요청 (이슈 #64)

### `POST /v1/user-memory`

확정된 하루 기록으로 사용자 압축 프로필을 갱신하는 작업을 접수합니다. 실제 처리는
백그라운드에서 진행됩니다.

**완료 콜백이 없습니다.** 결과 저장 API 한 번이 결과 전달과 종료 통보를 겸하며
성공·실패 모두 그 경로로 나갑니다(5.4 참고).

```text
앱 → App Server   일기 저장 (DailyRecord DRAFT → SAVED)
App Server → AI   POST /v1/user-memory                            → 202 Accepted
AI → App Server   POST /user-memory/updates/{taskId}/result       (성공·실패 공통)
```

### Request Body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `taskId` | `string` | O | 작업 식별자입니다. 형식은 검증하지 않습니다. |
| `taskToken` | `string` | O | 이 작업의 토큰입니다. **갱신되지 않습니다** — 결과 저장이 유일한 호출이라 갱신 기회가 없습니다. |
| `userMemory` | `object \| null` | O | 기존 User Memory입니다. 최초 생성이면 `null`입니다. |
| `diaries` | `object[]` | O | 확정된 하루 기록입니다. 비어 있어도 됩니다. |
| `diaries[].date` | `string` | O | 대상 날짜(`YYYY-MM-DD`)입니다. |
| `diaries[].recordTimeZone` | `string` | | 시간대입니다. 기본값은 `Asia/Seoul`입니다. |
| `diaries[].emotionType` | `string \| null` | | 현재 항상 `null`입니다. 받아만 두고 사용하지 않습니다. |
| `diaries[].events[].eventType` | `string` | O | **자유 문자열**입니다. enum으로 제한하지 않습니다. |
| `diaries[].events[].title` | `string` | | AI가 쓴 제목입니다. |
| `diaries[].events[].subtitle` | `string \| null` | | AI가 쓴 부제입니다. |
| `diaries[].events[].question` | `string \| null` | | AI가 붙인 회고 유도 질문입니다. |
| `diaries[].events[].memo` | `string \| null` | | **사용자가 직접 쓴 메모**입니다. 500자로 자릅니다. |
| `diaries[].events[].startAt` | `datetime` | O | 시작 시각입니다. |
| `diaries[].events[].endAt` | `datetime \| null` | | 종료 시각입니다. 단일 시점 event는 `null`입니다. |

### Request Example

```json
{
  "taskId": "0198f2a1-7c3d-7000-8b2e-1f4a9c05d6e7",
  "taskToken": "task-token-001",
  "userMemory": null,
  "diaries": [
    {
      "date": "2026-08-04",
      "recordTimeZone": "Asia/Seoul",
      "emotionType": null,
      "events": [
        {
          "eventType": "MEAL",
          "title": "회사 근처에서 점심을 먹었어요",
          "subtitle": null,
          "question": "그 자리에서 어떤 이야기가 기억에 남았나요?",
          "memo": "오랜만에 팀 사람들이랑 이야기를 많이 했다.",
          "startAt": "2026-08-04T12:10:00+09:00",
          "endAt": "2026-08-04T13:00:00+09:00"
        }
      ]
    }
  ]
}
```

전체 예시는 [docs/samples/user-memory-update.sample.json](samples/user-memory-update.sample.json)에 있습니다.

### Success Response

#### `202 Accepted`

```json
{
  "taskId": "0198f2a1-7c3d-7000-8b2e-1f4a9c05d6e7",
  "status": "PROCESSING"
}
```

### 크기로 거절하지 않습니다

접수는 **스키마만 맞으면 항상 202**입니다. 이벤트가 많은 하루도 거절하지 않습니다.

AI 서버가 4xx를 내면 App Server는 이를 "미접수 확정"으로 보고 작업을 폐기한 뒤 앱에
502를 돌려줍니다. 즉 사용자에게는 *일기 저장 실패*로 보입니다. 정상적인 하루가 그렇게
보이면 안 되므로, 입력이 크면 거절 대신 **프롬프트 조립 단계에서 자릅니다.**

| 대상 | 상한 | 초과 시 |
|---|---|---|
| `diaries` | 7일 | 오래된 날부터 제외 |
| `events` 총합 | 50개 | **메모 있는 event를 남기고** 오래된 것부터 제외 |
| `memo` | 500자 | 잘라서 사용 |
| `title`, `subtitle` | 255자 | 잘라서 사용 |

무엇을 얼마나 잘랐는지는 운영 이벤트(`usermemory.task.completed`)의
`droppedDiaryCount`/`droppedEventCount`에 남습니다.

422는 계약 위반일 때만 나갑니다 — 필수 필드 누락, `startAt` 파싱 실패 등입니다.

### 처리 시간

전체 처리에 `USER_MEMORY_TIMEOUT_SEC`(기본 120초) 예산을 둡니다. 초과해도 `FAILED`
결과는 보냅니다.

## 5. AI 서버가 호출하는 App Server API

AI 서버는 설정된 `APP_SERVER_API_URL`에 task별 경로를 붙여 네 개의 API를 호출합니다.

```env
APP_SERVER_API_URL=https://api.example.com/s/api/v1
```

| 작업 | 순서 | Method | 경로 |
|---|---|---|---|
| 타임라인 | 1 | `GET` | `{APP_SERVER_API_URL}/timeline/drafts/{taskId}/input` |
| 타임라인 | 2 | `POST` | `{APP_SERVER_API_URL}/timeline/drafts/{taskId}/result` |
| 타임라인 | 3 | `POST` | `{APP_SERVER_API_URL}/timeline/drafts/{taskId}/callback` |
| User Memory | 1 | `POST` | `{APP_SERVER_API_URL}/user-memory/updates/{taskId}/result` |

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
  "userMemory": {
    "schemaVersion": "1.0",
    "updatedAt": "2026-07-20T21:00:00+09:00",
    "basicProfile": "경기도에 사는 20대 후반 개발자입니다.",
    "routines": "평일 아침에 출근하고 저녁에는 회고를 적습니다.",
    "customAttributes": {
      "자주 가는 카페": "회사 근처 1층 카페"
    }
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
| `userMemory` | `object \| null` | **선택**입니다. 사용자 압축 프로필 v1.0이며, 없으면 User Memory 없이 생성합니다. |
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

#### `userMemory` (선택, 이슈 #65)

사용자의 생활 단계·관계·성향·관심사·기억 방식을 담은 압축 프로필입니다. 하루치
수집 원본과 달리 **사건 데이터가 아니라 해석과 표현을 돕는 보조 context**입니다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `schemaVersion` | `string` | `"1.0"`만 지원합니다. |
| `updatedAt` | `string \| null` | 마지막 갱신 시각입니다. 프롬프트에는 싣지 않습니다. |
| `basicProfile`, `lifeContext`, `relationships`, `personality`, `values`, `preferences`, `routines`, `currentFocus`, `emotionalPatterns`, `memoryStyle` | `string` | 자연어 필드이며 각 **최대 200자**입니다. 비어 있으면 프롬프트에서 생략합니다. |
| `customAttributes` | `object` | 고정 필드로 담기지 않는 값입니다. **최대 5개**, 값당 **최대 150자**입니다. |

최상위 필드는 고정입니다. 위 목록에 없는 최상위 필드, 지원하지 않는
`schemaVersion`, 길이·개수 초과는 계약 위반입니다.

**계약 위반은 타임라인을 실패시키지 않습니다.** 오류 코드 `1106`으로 기록하고
User Memory 없이 생성을 계속합니다(흡수). 보조 context 하나 때문에 하루치 수집
원본을 버리지 않기 위해서입니다. 필드가 없거나 `null`이어도 같은 경로로,
User Memory 없이 처리합니다.

User Memory 본문은 운영 로그와 관측에 남기지 않습니다. 남는 것은 `hasUserMemory`,
`schemaVersion`, 채워진 필드 수, 직렬화 크기 같은 비식별 메타데이터뿐입니다.
Langfuse `generation input`(프롬프트 본문)에는 값이 들어가는데, 운영 환경의
콘텐츠 정책이 `NONE`이라 본문이 밖으로 나가지 않습니다.

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
      "sourceRawIds": ["b1f0b6d5-5c3e-4a4e-9a37-2f1f0d2f3b71"],
      "question": "그날 점심 자리에서 어떤 이야기가 가장 기억에 남았나요?"
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
| `question` | `string \| null` | 사용자에게 보여 줄 회고 유도 질문입니다. 이벤트마다 하나씩 붙으며, 생성하지 못한 경우에만 `null`입니다. 255자로 자릅니다. |

`eventType` 값:

```text
WAKE_UP, SLEEP, MOVEMENT, CALENDAR_EVENT, MEAL, PHOTO_MOMENT,
MEETING, CLASS, WORK, EXERCISE, SOCIAL, REST, UNKNOWN
```

- 성공 응답은 `200 OK`이며 body는 사용하지 않습니다.
- 입력에 없는 `rawId`가 결과에 남아 있으면 AI 서버가 저장 요청 자체를 보내지 않고
  `1301`로 실패 처리합니다.
- 이벤트가 0건이어도 요청을 보냅니다. "생성 결과 없음"도 확정된 결과입니다.

#### `question` (이슈 #66)

이벤트마다 사용자가 자기 경험을 덧붙이도록 유도하는 질문 하나가 붙습니다.
**질문 목록을 최상위에 두지 않고 이벤트 안에 중첩합니다** — 이 계약에는
`clientEventId`가 없어 최상위 목록이 이벤트를 안정적으로 가리킬 수 없습니다.

- **모든 이벤트가 질문 하나를 갖습니다.** 이벤트 종류에 따른 예외는 없습니다.
- 1차 응답에서 빠진 이벤트는 한 번 더 요청합니다. 그래도 채우지 못하면 그 이벤트만
  `null`로 나갑니다 — 질문 하나 때문에 하루 기록 저장을 막지 않습니다. 그래서 필드
  타입은 `string | null`입니다.
- 질문 생성이 실패해도 타임라인 저장은 그대로 진행되며 모든 `question`이 `null`이
  됩니다. 이 실패로 task가 `FAILED`가 되지는 않습니다(오류 코드 `1209`, 흡수).

App Server 저장 스키마에는 이 필드가 이미 있습니다(2026-08-05 확인). AI 서버가
`question`을 보내기 시작해도 받는 쪽이 준비돼 있으므로 배포 순서 제약은 없습니다.

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

### 5.4 User Memory 결과 저장 (이슈 #64)

```http
POST {APP_SERVER_API_URL}/user-memory/updates/{taskId}/result
Task-Token: {taskToken}
```

**이 호출 하나가 결과 전달과 종료 통보를 겸합니다.** 성공도 실패도 같은 경로로
나가며, 완료 콜백은 없습니다.

#### 성공

```json
{
  "status": "SUCCESS",
  "userMemory": {
    "schemaVersion": "1.0",
    "updatedAt": "2026-08-06T09:00:00+09:00",
    "basicProfile": "경기도에 사는 20대 후반 개발자입니다.",
    "lifeContext": "",
    "relationships": "",
    "personality": "",
    "values": "",
    "preferences": "",
    "routines": "평일 아침에 출근하고 저녁에는 회고를 적습니다.",
    "currentFocus": "",
    "emotionalPatterns": "",
    "memoryStyle": "",
    "customAttributes": {}
  }
}
```

#### 실패

```json
{
  "status": "FAILED",
  "errorCode": 1210,
  "error": "사용자 메모리 생성에 실패했습니다."
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | `string` | `SUCCESS` 또는 `FAILED`입니다. |
| `userMemory` | `object \| null` | 전체 갱신본입니다. 실패는 `null`입니다 — 부분 결과를 저장하지 않습니다. |
| `errorCode` | `int \| null` | 실패 원인 정수입니다. 성공은 `null`입니다. |
| `error` | `string \| null` | 카탈로그의 안전 메시지입니다. 성공은 `null`입니다. |

- `schemaVersion`과 `updatedAt`은 **AI 서버가 확정**합니다. LLM이 만든 값을 쓰지 않습니다.
- 응답은 상태 코드만 봅니다. `2xx`가 성공이고, 재시도·중단 규칙은 타임라인 경로와 같습니다.
- `taskToken`은 갱신되지 않습니다. 접수 요청 body의 값을 끝까지 사용합니다.

##### `FAILED`의 의미 — `SAVED` 전이와 분리됩니다

`FAILED`는 **"User Memory가 바뀌지 않았다"**는 뜻이지 "하루 기록 저장이 실패했다"가
아닙니다. `DailyRecord`의 `DRAFT → SAVED` 전이는 앱 → App Server 구간에서 이미 끝나
있습니다. 둘을 한 트랜잭션으로 묶으면 AI 실패가 사용자의 일기 저장을 되돌리게 되고,
"실패해도 사용자 피해가 없다"는 전제가 사라집니다.

실패 원인별 코드는 다음과 같습니다.

| 실패 원인 | `errorCode` |
|---|---|
| AI 응답 스키마 검증 실패 | `1202` |
| LLM provider 호출 실패 | `1203` |
| 갱신본 생성 실패(제한 시간 초과 포함) | `1210` |
| 크기·민감정보 검증을 재요청 뒤에도 통과 못 함 | `1304` |

기존 `userMemory`가 v1.0 계약을 어긴 경우는 실패가 아닙니다. 코드 `1106`으로 기록하고
**새로 만들어** 대체합니다 — 여기서 멈추면 그 사용자는 이후 어떤 날도 갱신되지
않습니다.

이 호출 자체가 재시도까지 실패하면(`1305`) App Server는 아무 연락도 받지 못하고 작업이
TTL로 정리됩니다. 콜백이 있어도 401/404/409에서는 같았으므로 회귀는 아닙니다.

## 6. 데이터 접근 경계

| 대상 | 소유 | AI 서버 접근 |
|---|---|---|
| 수집 원본 | App Server | 입력 조회 API (읽기) |
| 타임라인 결과 | App Server | 결과 저장 API (쓰기) |
| User Memory | App Server | 입력 조회 API (읽기), 갱신 결과 저장 API (쓰기) |
| 작업 상태 | App Server | 콜백(타임라인) 또는 결과 저장(User Memory)으로 통보만 |

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
| `PROCESSING` | 접수 응답(타임라인·User Memory) | 요청이 접수되어 백그라운드 처리를 시작함 |
| `SUCCESS` | 완료 콜백 | 타임라인 생성과 결과 저장이 완료됨 |
| `FAILED` | 완료 콜백 | 입력 조회, AI 처리, 검증 또는 결과 저장에 실패함 |
| `SUCCESS` | User Memory 결과 저장 | 갱신본을 만들었고 함께 보냄 |
| `FAILED` | User Memory 결과 저장 | User Memory가 바뀌지 않음(하루 기록 저장과 무관) |

## 9. API 문서 확인

서버 실행 후 다음 경로에서 자동 생성된 문서를 확인할 수 있습니다.

| 문서 | 경로 |
|---|---|
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI JSON | `/openapi.json` |
