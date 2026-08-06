# 공통 언어

## Scope

코드·API·문서에서 같은 이름으로 써야 하는 핵심 도메인 용어와 서로 혼동하기 쉬운 개념의 경계를 정의한다.

## Read When

- schema, class, field, endpoint, log 이름을 만들거나 바꿀 때
- Timeline 생성 흐름을 설명하거나 이슈·PR을 작성할 때
- `question`, `event`, `source`, `task`처럼 중의적인 말을 사용할 때

## Authoritative Sources

- `app/schemas/**`의 모델·enum·field alias
- `app/api/v1/timeline.py`, `app/services/timeline_runner.py`, `app/services/app_server_client.py`
- `app/agents/**`, `app/services/**`
- `tests/**`의 계약 이름

## Current Implementation

| 용어 | 이 프로젝트에서의 뜻 |
|---|---|
| Timeline task | `taskId`로 상관되는 비동기 생성 작업. 상태와 수명은 App Server가 소유한다. |
| Timeline trigger | `taskId`, 최초 `taskToken`, `dailyRecordId`, 정본 `window`를 전달하는 202 접수 요청. source 본문은 싣지 않는다. |
| `dailyRecordId` | App Server의 일별 기록 연결 값. AI 서버에서는 관측 상관값과 runner 인자로만 쓰며 결과 body에 싣지 않는다. |
| Timeline window | 생성 대상 시간 범위. input response에도 있을 수 있지만 접수 request의 값이 정본이다. |
| Collected snapshot | App Server input response를 pipeline 내부 형태로 옮긴 하루 source 묶음. 평평한 `sourceItems`를 가진다. |
| Source item | 센서·일정·알림·사진 원본 한 건. `itemType`, `rawId`, 시간, type별 payload로 구성된다. |
| `rawId` | App Server 원본의 정식 UUID 식별자. candidate, fragment, event 근거와 result 연결에 사용한다. |
| `itemType` | source payload domain을 결정하는 분류. 현재 STAY, MOVEMENT, CALENDAR, HEALTH, NOTIFICATION, PHOTO다. |
| Normalized request | snapshot을 domain별 list로 분리·검증한 `TimelineDraftRequest`. 모든 Agent의 공통 입력이다. |
| Event Agent | 한 source domain을 사실 중심으로 해석해 candidate와 fragment를 만드는 Agent. |
| Candidate | event로 볼 근거가 충분한 중간 산출물. 아직 최종 Timeline event가 아니다. |
| Fragment | candidate로 확정하기에는 약하지만 다른 source와 결합할 수 있는 단서. 원본 단순 요약과 동의어가 아니다. |
| SourceRef | candidate 또는 event가 어떤 source `rawId`를 왜 근거로 삼는지 나타내는 참조. `sourceType` label은 실제 입력 type으로 정정될 수 있다. |
| Timeline Agent | 여러 candidate/fragment를 의미적으로 합쳐 넓은 `TimelineDraft`를 만드는 Agent. 결과는 아직 확정 전이다. |
| Timeline draft | event, 내부 모호성 질문, warning과 판단 metadata를 담는 편집 가능한 내부 결과. App Server 저장 request보다 넓다. |
| Timeline event | 사용자가 읽는 하루의 사건 단위. source에 근거해야 하며 Repair 뒤 시간순 ID를 갖는다. |
| `clientEventId` | 현재 draft 안에서만 쓰는 `event-NNN` 식별자. 병합·삭제 뒤 코드가 다시 부여하며 App Server result에는 보내지 않는다. |
| Repair Agent | draft를 코드로 확정하고 남은 의미 문제를 LLM tool plan으로 제한 횟수 개선하는 Agent. |
| Confirm/확정 pass | `repair_draft`와 fragment 검사를 실행해 source·시간·정렬·ID 등 결정론 규칙을 재적용하는 단계. |
| Guard | 특정 불변식을 검사·보정하거나 warning으로 드러내는 결정론 service. 모든 guard가 값을 자동 수정하는 것은 아니다. |
| Question Agent | Repair가 확정한 event에 사용자 회고 유도 질문을 선택적으로 붙이는 Agent. |
| 내부 모호성 질문 | `TimelineDraft.questions`. 불확실한 시간·장소를 확인하는 내부 draft 정보이며 App Server로 보내지 않는다. |
| 회고 유도 질문 | `TimelineEventDraft.question`/result event의 `question`. 사용자가 경험·감정·이유를 덧붙이도록 event에 중첩해 저장하는 질문. |
| Warning | 복구 가능한 누락·충돌·품질 문제를 드러내는 내부 진단. task 실패와 동의어가 아니다. |
| Confidence | event/candidate 확신도를 0~1로 표현한 값. 불확실성을 문장에 헤지하는 대신 metadata로 전달한다. |
| Inference level | DIRECT, EVIDENCE_BASED, INFERRED, UNCERTAIN으로 판단 근거 수준을 표현한다. |
| User Memory | 사용자를 압축한 프로필 v1.0. 사건 데이터가 아니라 해석·표현을 돕는 보조 context다. 소유는 App Server, 소비는 Timeline·Question Agent, 생성은 User Memory Agent다. |
| User Memory 갱신 task | 확정된 하루 타임라인으로 프로필 전체를 다시 쓰는 비동기 작업. Timeline task와 별개이며 callback이 없다. |
| Daily timeline | 갱신 입력의 하루치 확정 타임라인(`dailyTimelines[]`). `date`와 `events`로 구성되며 수집 원본(source item)이 아니라 **이미 사용자에게 보인 결과**다. |
| Daily timeline event | Daily timeline 안의 event 한 건. `title`·`subtitle`·`question`은 AI가 쓴 문장이고 `memo`만 사용자가 직접 쓴 글이다. |
| `memo` | 사용자가 event에 직접 남긴 글. 성향 계열 필드의 **유일한** 근거이며 비어 있을 수 있다. |
| 갱신본(rewrite) | 기존 프로필을 통째로 대체하는 새 User Memory 전체 문서. append가 아니다. |
| App Server | source, Timeline 결과 persistence, User Memory, task 상태를 소유하는 외부 서버. AI 서버의 제품 데이터 경계다. |
| TaskToken | 한 task의 App Server 서버간 인증 token holder. 값 자체가 아니라 최신 값과 갱신 횟수 개념을 구분한다. |
| Callback | 결과 body 전달이 아니라 SUCCESS/FAILED terminal 상태 통보다. 결과는 그 전에 result API로 저장한다. **Timeline에만 있다** — User Memory 갱신은 결과 저장 한 번이 통보를 겸한다. |
| 운영 이벤트 | FastAPI 운영 집계를 위해 allowlist된 stdout JSON event. Agent trace와 다르다. |
| Langfuse trace | Agent tree, LLM generation, token·latency와 policy에 따른 본문을 담는 선택적 AI 실행 관측. |

## Invariants

- “질문”을 쓸 때 내부 모호성 질문인지 회고 유도 질문인지 구분한다.
- “저장”은 App Server result 제출을 뜻하며 callback과 구분한다.
- “source ID”는 특별한 설명이 없으면 rawId를 뜻한다.
- “task 상태”와 프로세스 로컬 inflight/busy를 같은 것으로 부르지 않는다.
- “Repair”는 LLM 개선만이 아니라 필수 결정론 확정 pass를 포함한다.

## Known Gaps

- 일부 오래된 docstring이 snapshot을 “DB에서 읽는다”고 표현하지만 현재 공식 경계는 App Server API다.
- `TimelineDraft.userId`는 현재 내부 placeholder이고 inbound identity나 result 계약과 연결되지 않는다.
- 한국어·영어 용어가 코드에 혼용돼 있으나 public JSON alias는 현재 camelCase 계약을 따른다.

## Update When

공식 모델·enum·외부 payload 의미가 달라지거나, 같은 이름으로 구분하던 개념이 합쳐지거나 새 도메인 핵심 용어가 생길 때 갱신한다.

## Validation

- `rg -n "class |class .*Enum|Field\(alias=" app/schemas app/api`
- 정의한 용어를 `rg -n "<용어>" app tests docs`로 실제 사용처 확인
- schema·result·Question 관련 테스트 실행

