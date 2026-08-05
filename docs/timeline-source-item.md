# AI 하루 타임라인 입력 Source Item 계약

이 문서는 현재 프로덕션 코드의 두 입력 경계를 정의한다.

1. `app/schemas/source_snapshot.py`의 `CollectedSnapshot`: DB에서 읽은 평평한 수집 원본.
2. `app/schemas/timeline_request.py`의 `TimelineDraftRequest`: normalizer가 도메인별로
   분리해 Main/Event/Timeline Agent에 전달하는 요청.

샘플은 Agent가 실제 소비하는 두 번째 계약을 표현한다:
[`samples/timeline-request.sample.json`](samples/timeline-request.sample.json).

## 처리 경계

```text
timeline_draft_source_items
→ CollectedSnapshot(sourceItems[])
→ normalize()
→ TimelineDraftRequest(stays/movements/...)
→ Main Agent
```

- 모든 source item의 유일한 식별자는 UUID `rawId`다.
- DB PK는 repository 내부 행 식별·진단에만 사용하며 snapshot과 Agent 요청에는 넣지 않는다.
- 시각은 epoch 숫자로 변환하지 않고 수집 원본의 ISO 문자열을 유지한다.
- `userMemory`는 rawId를 가진 source item이 아니라 해석용 보조 context다. 형태는
  고정 스키마 v1.0이며 계약은 [ai-server-api.md §5.1](ai-server-api.md#51-입력-조회)에 있다.

## 1. CollectedSnapshot

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `taskId` | string | ✅ | 처리 작업 식별자 |
| `recordDate` | string | ✅ | 수집 기준 날짜/시각 |
| `recordTimeZone` | string | 선택 | 기본값 `Asia/Seoul` |
| `timelineWindow.startTime` / `endTime` | string | 선택 | 처리 대상 ISO 시간 경계 |
| `sourceItems` | array | 선택 | 평평한 수집 항목 목록 |
| `userMemory` | object | 선택 | 사용자 압축 프로필 v1.0 (보조 context) |

### CollectedSourceItem 공통 필드

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `rawId` | UUID string | ✅ | 원본 source 식별자 |
| `itemType` | enum | ✅ | `STAY`, `MOVEMENT`, `CALENDAR`, `HEALTH`, `NOTIFICATION`, `PHOTO` |
| `startAt` | string | ✅ | ISO 시각 문자열 |
| `endAt` | string/null | 선택 | 구간 종료 시각 |
| `payload` | object | 선택 | itemType별 원본 필드 |

repository는 한 task의 행들이 동일 user에 속하는지, `start_at`과 `raw_id`가 존재하는지,
rawId가 task 안에서 중복되지 않는지 확인한다. normalizer는 itemType에 따라 아래 도메인
모델로 변환하며, 개별 항목이 해당 모델 검증에 실패하면 그 항목만 제외하고 로그를 남긴다.

## 2. TimelineDraftRequest

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `taskId` | string | ✅ | snapshot과 동일한 작업 식별자 |
| `date` | string | ✅ | `recordDate` 앞 10자리 |
| `timezone` | string | 선택 | 기본값 `Asia/Seoul` |
| `window.start` / `window.end` | string | 선택 | snapshot window를 옮긴 ISO 문자열 |
| `stays` | `StayItem[]` | 선택 | 기본 빈 배열 |
| `movements` | `MovementItem[]` | 선택 | 기본 빈 배열 |
| `calendars` | `CalendarItem[]` | 선택 | 기본 빈 배열 |
| `healths` | `HealthItem[]` | 선택 | 기본 빈 배열 |
| `notifications` | `NotificationItem[]` | 선택 | 기본 빈 배열 |
| `photos` | `PhotoItem[]` | 선택 | 기본 빈 배열 |
| `userMemory` | object | 선택 | 사용자 압축 프로필 v1.0 (보조 context) |

## 도메인 항목

모든 도메인 항목은 UUID `rawId`를 필수로 가진다.

### StayItem

`rawId`, `startAt`, `endAt?`, `latitude?`, `longitude?`, `place?`, `address?`,
`places[]`, `durationText?`

### MovementItem

`rawId`, `startAt`, `endAt?`, `start?`, `end?`, `durationText?`,
`distanceMeters?`, `transports[]`

`start`와 `end`는 `latitude?`, `longitude?`, `place?`, `address?`, `places[]`를 갖는
`GeoPlace`다.

### CalendarItem

`rawId`, `startAt`, `endAt?`, `title`, `description?`, `locationText?`, `allDay`

### HealthItem

`rawId`, `metric`, `startAt`, `endAt?`, `value?`, `durationMinutes?`

현재 metric 허용값은 `STEPS`, `SLEEP`이다. 수집 payload의 숫자가 문자열이면 normalizer가
숫자 부분을 추출하며, SLEEP 문자열 값은 `durationMinutes`로 옮긴다.

### NotificationItem

`rawId`, `postedAt`, `appName`, `title`, `text`

### PhotoItem

`rawId`, `takenAt`, `dateTaken?`, `latitude?`, `longitude?`, `description?`,
`photoUrl?`

`photoUrl`은 App Server가 주는 S3 이미지 URL이다. Photo Agent가 이 URL에서 실제
이미지를 내려받아 vision 모델로 `description`을 만든다. presigned URL이면 query에
서명 자격증명이 실리므로 **직렬화에서 제외**(`exclude=True`)되며, 프롬프트·운영
로그·Langfuse 어디에도 값이 나가지 않는다.

`fileName`·`clientPhotoUri`·`photoFile`은 payload에 와도 무시한다. 각각 스토리지
객체 이름(UUID)과 클라이언트 내부 URI(`content://…`)라 AI가 쓸 정보가 없고,
프롬프트에 들어가면 LLM이 의미를 지어낼 여지만 생긴다(이슈 #52).

## 검증 책임

- repository: task 소속, 단일 user, `start_at`·`raw_id` 존재, task 내 rawId 중복 금지.
- Pydantic 도메인 모델: rawId UUID, 필수 필드, enum, 좌표와 음수가 될 수 없는 수치 범위.
- Event Agent 경계: 출력 rawId를 현재 요청 allowlist와 대조해 잘못된 참조를 제거한다.
- Repair 경계: Timeline draft를 같은 allowlist로 다시 검증하고 근거 없는 event를 제외한다.
- 저장 직전: 모든 sourceRefs.rawId가 해당 task의 실제 source row에 속하는지 다시 확인한다.
- 같은 실제 source는 여러 event의 근거가 될 수 있다. event↔source 관계는 N:M이다.
- `sourceType`은 rawId로 찾은 실제 입력 타입으로 정정한다.
- `userMemory`는 보조 context이며 `sourceRefs`에 넣지 않는다.

