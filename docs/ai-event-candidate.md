# AI Event 후보 모델

이슈 #8 — 데이터별 Agent 가 반환할 공통 **AI Event 후보**를 정의한다.
모델은 `app/schemas/event_candidate.py` 의 `AiEventCandidate` 이며,
샘플은 [`samples/ai-event-candidate.sample.json`](samples/ai-event-candidate.sample.json) 에 있다.

## 위치

- AI Event 후보는 **최종 timeline event 가 아니다.**
  위치·캘린더·사진·수면·활동·알림 등 데이터별 Agent 가 각자의 source item 을 해석해
  내놓는 **후보**다.
- Timeline Agent 가 여러 Agent 의 후보를 **병합·분할·검증**해 최종 draft 를 만든다.
- 후보에는 아직 **안정적인 `eventId` 가 없다.** 최종 `eventId` / `clientEventId` 는
  Timeline Agent 가 draft 를 만들 때 부여된다. run 내부 추적이 필요하면 DTO 본질
  필드가 아니라 `candidateIndex` / `traceId` 같은 별도 수단으로 다룬다.

## 필드

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `eventType` | enum | ✅ | 후보 활동 종류 (아래 목록) |
| `timeRange.startTime` / `timeRange.endTime` | ISO 8601 datetime(tz 포함) | ✅ | 후보 시간 구간. 순간 이벤트는 두 값이 같다 |
| `title` | string | ✅ | 사람이 읽을 후보 제목 |
| `description` | string | ✅ | 후보 근거·판단 설명 |
| `sourceRefs` | array | ✅(최소 1) | 근거가 된 source 참조 목록 |
| `sourceRefs[].sourceType` | enum | ✅ | 근거 데이터 도메인 (아래 목록) |
| `sourceRefs[].rawId` | UUID string | ✅ | 수집 원본의 안정적 식별자 |
| `confidence` | float(0.0~1.0) | ✅ | 후보 확신도 |
| `inferenceLevel` | enum | ✅ | 추론 수준 (아래 목록) |
| `uncertainty` | string[] | 선택 | 불확실 요인 메모 (비면 없음) |

> 입력 Source Item과 AI Event 후보의 시각은 모두 ISO 문자열을 사용한다.
> 후보의 `timeRange`는 Pydantic `AwareDatetime`으로 검증하므로 timezone을 반드시
> 포함해야 한다.

## eventType 목록

`WAKE_UP`, `SLEEP`, `MOVEMENT`, `CALENDAR_EVENT`, `MEAL`,
`PHOTO_MOMENT`, `MEETING`, `CLASS`, `WORK`, `EXERCISE`, `SOCIAL`, `REST`, `UNKNOWN`

- UI 타입 확정이 아니라 **Timeline Agent 가 병합·해석하기 쉽게 분류**하는 것이 목적이다.
- `STAY` 는 eventType 이 아니다. 체류는 센서 상태이지 사건이 아니므로, 체류의 **의미**로
  분류한다. `sourceType` 의 `STAY` 와 혼동하지 않는다.
- 근거가 약하면 단정하지 말고 더 굵은 종류(예: `REST`)나 낮은 confidence 로 둔다.
  - 예: 위치만으로 식당 체류가 잡히면 바로 `MEAL` 로 단정하지 않는다. `REST` 또는
    낮은 confidence 의 `MEAL` 후보로 두고, 음식 사진·결제·캘린더 등 근거가 합쳐질 때
    Timeline Agent 가 최종 draft 에서 `MEAL` 로 확정한다.
- `MEAL` 은 보통 20~60분이다. 식당·카페에 오래 머물렀더라도 그 체류 전체를 `MEAL` 로
  만들지 않는다. 긴 체류는 체류대로 두고, 음식 사진·결제 알림 같은 **시점 근거** 부근의
  짧은 식사 event 를 따로 둔다. `app/services/meal_guard.py` 가 60분을 넘는 `MEAL` 을
  결정론적으로 잘라 낸다.
- 어디에도 맞지 않으면 `UNKNOWN` 으로 둔다.

## sourceRefs 구조

이 후보가 어떤 원천/파생 데이터에 근거했는지 나타내는 핵심 필드다.

```json
{ "sourceType": "PHOTO", "rawId": "44444444-4444-4444-8444-444444444444" }
```

`sourceType` 값(굵은 카테고리): `STAY`, `MOVEMENT`, `CALENDAR`, `PHOTO`, `SLEEP`,
`ACTIVITY`, `NOTIFICATION`

- 입력 계약(#7)의 세분화된 `SourceType`(예: `stay`)과 다른, **도메인 단위**다.
- `rawId` 는 수집 원본의 안정적 UUID 식별자다. Reflection/Repair 단계에서
  "이 후보를 다시 봐라"가 아니라 "이 시간대의 이 source 들을 다시 분석하라"로 범위를
  좁히는 핸들이라 중요하다.

### rawId 는 정식 식별자, sourceType 은 정정 대상

`rawId`는 입력에 실재하는 UUID여야 한다. 내부 DB PK인 `id`는 Agent 입력과 출력 계약에
포함하지 않으며 `rawId`의 대체값으로도 사용하지 않는다.

Event Agent 출력과 Timeline draft는 입력 rawId allowlist와 대조한다. LLM이 입력에 없는
rawId를 만들면 해당 참조를 제거하고, 유효한 근거가 하나도 남지 않은 후보·fragment·event는
결과에서 제외한 뒤 warning을 남긴다.

같은 source가 서로 다른 여러 사건을 실제로 뒷받침하면 동일 rawId를 여러 event가
공유할 수 있다. 저장 관계도 event↔source N:M이며, event별 `reason`으로 해당 source가
각 사건의 근거가 되는 이유를 구분한다.

다만 **`sourceType` 라벨은 믿지 않는다.** 실제 출력에서 LLM 은 왕복 이동의 rawId 세 개를
정확히 인용해 놓고 그중 둘을 `STAY` 라고 적었다. rawId 는 UUID 라 타입을 가로질러
유일하므로, `app/services/source_lookup.py` 가 repair 진입 시 각 sourceRef 의
`sourceType` 을 **입력의 실제 타입으로 정정**한다. 이것은 검증이 아니라 조회다 — 아무것도
버리지 않고 경고도 남기지 않는다. 라벨을 그대로 두면 이후 단계가 그 근거를 영영 찾지 못해,
산책 event 가 왕복 이동을 근거로 대 놓고도 편도에서 끊긴다.

하나의 `HEALTH` 입력은 `metric` 에 따라 `SLEEP` / `ACTIVITY` 로 나뉜다.
`userMemory`는 rawId를 가진 source item이 아니라 해석용 보조 context이므로
`sourceRefs`의 `sourceType`으로 사용하지 않는다.

## confidence 기준

0.0 ~ 1.0 점수로 둔다.

| 범위 | 의미 |
| --- | --- |
| 0.85 ~ 1.00 | 직접 근거가 강하거나 여러 source 가 일치함 |
| 0.65 ~ 0.84 | 근거는 충분하지만 일부 해석이 필요함 |
| 0.40 ~ 0.64 | 가능성은 있으나 확정하기 어려움 |
| 0.00 ~ 0.39 | 후보로만 보관하거나 question/warning 대상 |

예시:

- 캘린더 일정 존재: 높음 (단, 실제 참석 여부는 별도 근거 필요)
- 수면 기록 기반 기상: 높음
- 위치 + 음식 사진 기반 식사: 높음
- 위치만 식당 근처 체류: 중간 또는 낮음
- 알림 내용만으로 만남 추정: 낮음

## inferenceLevel 기준

| 값 | 의미 | 예 |
| --- | --- | --- |
| `DIRECT` | source 가 직접 말해주는 사실 | 캘린더 일정 존재, 사진 촬영 시각, 수면 시작/종료 |
| `EVIDENCE_BASED` | 여러 근거를 조합한 판단 | 식당 체류 + 음식 사진 → 저녁 식사 후보 |
| `INFERRED` | 근거는 있으나 해석 비중이 큰 판단 | GPS 속도·경로로 버스 이동 추정 |
| `UNCERTAIN` | 근거가 약하거나 충돌해 확정하면 위험한 후보 | 사진 위치와 실제 위치 로그가 충돌함 |

사용자 수정/추가 내용은 이 모델에 넣지 않는다. 이 모델은 **초기 AI 추론 후보**를
위한 것이고, 사용자 편집은 별도 source/edit 정보로 다룬다.

## source type 별 후보 예시

### 위치 — `REST`

```json
{
  "eventType": "REST",
  "timeRange": {
    "startTime": "2026-06-20T11:00:00+09:00",
    "endTime": "2026-06-20T18:00:00+09:00"
  },
  "title": "경북대학교 체류",
  "description": "위치 기록상 경북대학교 부근에 머문 것으로 보입니다.",
  "sourceRefs": [{ "sourceType": "STAY", "rawId": "11111111-1111-4111-8111-111111111111" }],
  "confidence": 0.82,
  "inferenceLevel": "EVIDENCE_BASED",
  "uncertainty": ["구체적인 활동은 위치 데이터만으로 확정할 수 없습니다."]
}
```

### 캘린더 — `CALENDAR_EVENT`

```json
{
  "eventType": "CALENDAR_EVENT",
  "timeRange": {
    "startTime": "2026-06-20T12:30:00+09:00",
    "endTime": "2026-06-20T13:30:00+09:00"
  },
  "title": "김종찬 멘토링",
  "description": "캘린더에 등록된 일정입니다.",
  "sourceRefs": [{ "sourceType": "CALENDAR", "rawId": "55555555-5555-4555-8555-555555555555" }],
  "confidence": 0.95,
  "inferenceLevel": "DIRECT",
  "uncertainty": ["일정이 존재하지만 실제 참석 여부는 다른 근거와 함께 확인해야 합니다."]
}
```

### 사진 — `PHOTO_MOMENT`

```json
{
  "eventType": "PHOTO_MOMENT",
  "timeRange": {
    "startTime": "2026-06-20T17:15:00+09:00",
    "endTime": "2026-06-20T17:15:00+09:00"
  },
  "title": "저녁 사진",
  "description": "음식 사진이 촬영되었습니다.",
  "sourceRefs": [{ "sourceType": "PHOTO", "rawId": "44444444-4444-4444-8444-444444444444" }],
  "confidence": 0.9,
  "inferenceLevel": "DIRECT",
  "uncertainty": ["사진만으로 식사 전체 시간이나 동행자는 확정할 수 없습니다."]
}
```

### 이동 — `MOVEMENT`

```json
{
  "eventType": "MOVEMENT",
  "timeRange": {
    "startTime": "2026-06-20T10:10:00+09:00",
    "endTime": "2026-06-20T11:00:00+09:00"
  },
  "title": "집에서 학교로 이동",
  "description": "위치 로그를 바탕으로 집에서 경북대학교까지 이동한 것으로 추정됩니다.",
  "sourceRefs": [{ "sourceType": "MOVEMENT", "rawId": "22222222-2222-4222-8222-222222222222" }],
  "confidence": 0.86,
  "inferenceLevel": "EVIDENCE_BASED",
  "uncertainty": ["이동 수단은 GPS 속도와 이동 경로를 바탕으로 추정되었습니다."]
}
```

## 과한 추론 방지 기준

- 캘린더 일정은 "일정 존재"이지 "실제 참석"이 아니다.
- 위치 체류는 "그 장소에 있었을 가능성"이지 "구체적 활동"이 아니다.
- 음식 사진은 식사 근거가 될 수 있지만, 동행자/감정/대화 내용은 단정하지 않는다.
- 알림 내용은 context 일 뿐 실제 행동을 확정하는 근거로 단독 사용하지 않는다.
- User Memory 는 보조 context 이며 실제 사건을 단독 확정하지 않는다.
- 근거가 충돌하면 confidence 를 낮추고 `uncertainty`, question, warning 으로 남긴다.
- 모르면 `UNKNOWN` 또는 낮은 confidence 후보로 두고 Timeline Agent / Reflection
  단계로 넘긴다.

### DTO validation 으로 강제하는 규칙

- **필수 필드**: `eventType`, `timeRange`, `title`, `description`, `sourceRefs`,
  `confidence`, `inferenceLevel` 이 없으면 거부된다.
- **sourceRefs**: 최소 1개. 각 항목의 `rawId` 는 UUID 형식이어야 한다.
- **confidence**: `0.0 ~ 1.0`.
- **timeRange**: `endTime >= startTime`, 두 값 모두 timezone 을 포함해야 한다.
- **uncertainty 강제**: `inferenceLevel` 이 `UNCERTAIN` 이면 `uncertainty` 근거를
  최소 1개 남겨야 한다(설명 없는 낮은 신뢰 후보 방지).

## 참고

- 최종 `eventId` 는 Timeline Agent 가 후보를 병합해 draft 를 만들 때 부여된다.
- 입력 Source Item 계약은 [`timeline-source-item.md`](timeline-source-item.md) 참고.


