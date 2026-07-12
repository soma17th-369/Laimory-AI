# AI 하루 타임라인 입력 Source Item 계약

이슈 #7 — AI 하루 타임라인 Agent 가 입력으로 받는 **Source Item 계약**을 정의한다.
요청 DTO 는 `app/schemas/timeline_request.py` 의 `TimelineDraftRequest` 이며,
샘플 요청은 [`samples/timeline-request.sample.json`](samples/timeline-request.sample.json) 에 있다.

## 전체 구조

하루 단위 라이프로그 raw snapshot 을 다음으로 표현한다.

```
기본 메타데이터
+ 위치 체류/이동 기록
+ 알림 수집 기록
+ 사진 메타데이터
+ 캘린더 일정
+ 건강 데이터
+ 사용자 메모리(보조 context)
```

- `transactionId` 는 이 스냅샷 **세트 전체**를 식별한다.
- 모든 source item 은 수집 주체가 전달한 안정적인 `sourceId` 를 가진다.
  `sourceId` 는 AI 결과 검증과 이후 선택적 재처리의 기준 키가 된다.
- 시각은 모두 Unix epoch **milliseconds** 정수다.

## 공통 필드 (모든 source item)

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `sourceId` | string | ✅ | 수집 주체가 전달하는 안정적 식별자 |
| `sourceType` | enum | ✅ | item 종류 (아래 표 참고) |

`sourceType` 값: `stay`, `movement`, `notification`, `photo`,
`calendar_event`, `health_steps`, `health_sleep`, `health_total_calories`,
`health_active_calories`, `health_distance`, `health_heart_rate`.

## 봉투(envelope) 필드

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `transactionId` | string | ✅ | 스냅샷 세트 전체 식별자 |
| `date` | string(`YYYY-MM-DD`) | ✅ | 수집 대상 날짜 |
| `mode` | enum(`FULL_DAY`) | ✅ | 수집 모드 |
| `window.start` / `window.end` | int(ms) | ✅ | 하루 수집 범위 |
| `generatedAt` | int(ms) | ✅ | JSON 생성 시각 |
| `sourceItems` | array | 선택 | STAY/MOVEMENT 등 수집 항목 목록 |
| `notifications` | object | 선택 | 알림 데이터 (기본 빈 active/collected) |
| `photos` | array | 선택 | 사진 목록 |
| `calendar` | object | 선택 | 캘린더 데이터 (기본 빈 events) |
| `health` | object | 선택 | 건강 데이터 |
| `userMemory` | object(비정형) | 선택 | 사용자 메모리 입력 |

## source type 별 payload

### 체류 — `sourceItems[]` (`STAY`)
`source`(str), `lat`, `lon`, `startTime`, `endTime`, `durationSec`

### 이동 — `sourceItems[]` (`MOVEMENT`)
`source`(str), `startTime`, `endTime`, `durationSec`, `distanceMeters`, `points[]`(`{lat, lon}`)

### 알림 — `notifications.active[]` / `notifications.collected[]` (`notification`)
`packageName`, `appName`, `title`, `text`, `postTime`, `collectedAt`

### 사진 — `photos[]` (`photo`)
`id`, `uri`, `dateTaken`, `lat`(선택), `lon`(선택), `width`, `height`, `mimeType`

### 캘린더 — `calendar.events[]` (`calendar_event`)
`title`, `startTime`, `endTime`, `location`(선택), `description`(선택)

### 건강 — `health.*`
- `steps` (`health_steps`): `count`
- `sleep` (`health_sleep`): `startTime`, `endTime`, `durationMinutes`
- `totalCaloriesBurned` (`health_total_calories`): `kcal`
- `activeCaloriesBurned` (`health_active_calories`): `kcal`
- `distance` (`health_distance`): `meters`
- `heartRate` (`health_heart_rate`): `samples[]`(`{time, bpm}`)

### 사용자 메모리 — `userMemory`
사용자가 확정한 기록을 바탕으로 새롭게 정의되는 **비정형 JSON**. key/value 는 AI 가
자동으로 분석해 채운다. 고정 스키마 없이 임의 key/value 를 허용하며, 타임라인 생성 요청에
함께 전달한다.

## validation 기준

DTO(`pydantic`) 에서 강제하는 규칙이다.

- **필수 필드**: 위 표의 필수 필드가 없으면 요청은 거부된다(422).
- **sourceId**
  - 모든 source item 에 필수, `min_length=1`.
  - 서버 DTO 에서는 sourceId 중복 여부를 검증하지 않는다.
  - `userMemory` 는 request 로 받지만 `sourceId`/`sourceType` 을 갖는 Source Item 이 아니다.
- **transactionId**: 필수, `min_length=1`.
- **date**: `YYYY-MM-DD` 형식.
- **mode**: `FULL_DAY` 등 정의된 값만 허용.
- **시각(ms)**: 모든 timestamp 는 `>= 0` 정수.
- **구간 시각**: `endTime >= startTime`(체류·이동·수면·일정), `window.end >= window.start`.
- **좌표**: `lat` 은 `-90 ~ 90`, `lon` 은 `-180 ~ 180`.
- **수치 범위**: `durationSec`, `distanceMeters`, `meters`, `kcal`, `count`, `durationMinutes`, `bpm` 은 `>= 0`; 이미지 `width`/`height` 는 `> 0`.

## 참고

- `sourceId` 는 AI 결과 검증과 이후 선택적 재처리에 필요하다.
- 사용자 메모리 데이터는 request 로 함께 받되, Source Item 계약 대상은 아니다.

