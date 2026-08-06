# 도메인 불변식

## Scope

Timeline 생성 결과가 의미와 근거를 보존하고 App Server·운영 계약을 지키기 위해 반드시 유지해야 하는 규칙을 모은다.

## Read When

- schema, prompt, Agent, Repair/guard, result/callback을 수정할 때
- “품질 개선”이 기존 근거·시간·보안 계약을 약화시키지 않는지 검토할 때
- 회귀 테스트 범위를 정할 때

## Authoritative Sources

- `app/schemas/**` model validator와 enum
- `app/services/source_contract.py`, `source_integrity.py`, `validator.py`, `draft_repair.py`, `timeline_validator.py`, `timeline_result.py`, 각 guard
- `app/agents/**/prompts/**`, Agent parse/fallback 코드
- `app/core/error_codes.py`, `redaction.py`, `operational_logging.py`
- `tests/**`, 특히 service·prompt·error·logging contract 테스트

## Current Implementation

### 입력·근거

- 한 task source batch의 taskId는 요청 taskId와 같고 source는 1건 이상이며 rawId는 서로 달라야 한다.
- rawId는 UUID이고 현재 request에 존재하는 값만 candidate, fragment, event 근거가 될 수 있다.
- 잘못된 sourceType label은 rawId로 찾은 실제 입력 type으로 정정한다.
- 유효한 sourceRef가 하나도 없는 candidate/event는 결과로 유지하지 않는다.
- source 하나가 여러 event의 근거가 되는 것은 허용한다.
- user memory는 근거가 아니라 해석·표현용 보조 context다. user memory만으로 사건 발생, 일정 참석, 장소, 이동 목적, 사람의 실명이나 정확한 관계를 확정하지 않고, 수집 원본과 충돌하면 원본이 이긴다. 이 경계는 prompt가 지키며 코드가 의미로 판정하지 않는다.
- user memory는 rawId를 갖지 않으므로 `sourceRefs`에 넣지 않는다.
- user memory는 해석·표현 계층(Timeline Agent, Question Agent)에만 주입한다. Event Agent와 Repair Agent는 받지 않는다. Event Agent 5종은 병렬로 돌고 Timeline이 결과를 병합하므로, 다섯이 같은 프로필을 읽으면 같은 근거 하나가 독립된 근거 다섯으로 세어진다.
- 소비 Agent 2종은 공용 projection 하나를 쓴다. Agent별로 필드를 골라 쓰거나 다르게 직렬화하지 않는다. 갱신 Agent가 "기존 프로필"을 읽을 때도 같은 projection이다.

### User Memory 갱신

- 갱신은 append가 아니라 전체 rewrite다. 출력이 기존 값을 통째로 대체한다.
- 입력의 `title`·`subtitle`·`question`은 이 시스템의 AI가 쓴 문장이다. 거기서 사용자 성향을 뽑지 않는다. 그렇게 하면 모델이 자기 출력을 읽고 사용자를 만들어 내는 되먹임이 되고, 그 프로필이 다시 다음 Timeline 문장을 만드는 데 쓰여 스스로를 강화한다.
- 성향 계열 다섯 필드(`personality`, `values`, `preferences`, `emotionalPatterns`, `memoryStyle`)의 근거는 `memo` 뿐이다. `memo`가 없는 날은 그 필드가 그대로인 것이 정상이고 결과는 `SUCCESS`다.
- 생활 구조 계열(`routines`, `lifeContext`, `currentFocus`)은 event 구조(시간대·종류·반복)를 근거로 갱신할 수 있다.
- `schemaVersion`과 `updatedAt`은 서버가 확정한다. LLM 출력값을 저장하지 않는다.
- 크기·민감정보를 어긴 갱신본은 코드가 자르지 않고 위반을 붙여 다시 요청한다. 재요청까지 실패하면 저장 문서를 만들지 않고 기존 값을 그대로 둔다.
- 위반 지적 문장은 prompt와 로그에 그대로 실리므로 걸린 값을 인용하지 않는다.
- 접수 입력은 크기로 거절하지 않고 prompt 조립 단계에서 자르며, 자를 때 `memo` 있는 event를 끝까지 남긴다. event 상한은 **날짜별 몫**이다 — 전체 상한 하나로 자르면 event가 몰린 하루가 다른 날의 자리를 다 먹고, 밀려난 날은 payload에서 빠져 모델에게는 없던 날이 된다. 무엇을 얼마나 잘랐는지는 운영 이벤트에 남긴다.
- 읽지 못하는 기존 프로필은 실패가 아니라 흡수다. 없는 셈 치고 새로 만든다 — 여기서 멈추면 그 사용자는 이후 어떤 날도 갱신되지 않는다.
- 갱신 실패는 하루 기록 저장(`DailyRecord`의 `DRAFT → SAVED`)과 분리된다. `FAILED`는 "User Memory가 바뀌지 않았다"는 뜻이다.

### 시간

- event/candidate의 end는 start보다 빠를 수 없다.
- 접수 request의 window가 정본이며 완전히 밖인 candidate/event는 제외하고 경계에 걸친 구간은 clamp한다.
- 수면 외 일반 event는 알려진 기상 경계 이전으로 확정하지 않는다. 경계를 알 수 없으면 시간을 지어내지 않는다.
- MEAL duration은 20~60분 범위로 제한하는 전용 guard가 맡는다.
- 비캘린더 장시간 event는 3시간 초과를 LOW warning으로 드러내되 코드가 임의 분할·절단하지 않는다. Calendar, Sleep, Movement, Meal은 이 검사에서 제외한다.
- Location-only event의 시간은 참조한 STAY/MOVEMENT 근거 밖을 주장하지 않도록 맞추되, 다른 source가 섞이면 그 source의 시간 의미를 존중한다.

### 보존·병합

- 모든 유효 Calendar item은 candidate와 최종 Timeline에서 누락되지 않도록 복원 대상이다.
- Event Agent에 들어온 source는 candidate 또는 fragment로 보존됐는지 검사한다.
- fragment만 근거로 남은 최종 event는 warning으로 드러낸다.
- 이동 없이 이어진 같은 장소 STAY는 순수 STAY event만 병합한다. Calendar·Photo·Notification 등이 섞인 사건을 긴 체류에 흡수하지 않는다.
- 같은 종류·장소이고 시간이 겹치는 중복 event는 병합할 수 있지만, 포함 관계나 서로 다른 사건의 부분 겹침을 무조건 잘라내지 않는다.
- Photo source는 최종 event 하나에만 귀속돼야 하며 코드가 의미를 모르면 임의 event에 재배치하지 않는다.

### 장소·민감정보

- place/address는 source 근거로 확정한다. 근거 없는 address를 유지하지 않는다.
- Photo vision이 직접 읽은 상호명은 Photo에 구조화 place field가 없어도 제한적으로 보존할 수 있다.
- Notification 원문의 개인정보·민감정보와 근거 없는 관계명은 candidate와 final draft 양쪽에서 검사한다.

### 최종 문장

- 최종 Timeline·Repair title/description은 사용자가 읽는 1인칭 해요체 과거형 일기다.
- title은 30자 이내 명사구, description은 1~2문장 100자 안팎을 목표로 하며 120자 초과는 warning이다.
- 최종 문장에 `듯해요` 같은 hedge와 분 단위 시각·걸음 수 같은 원본 수치를 쓰지 않는다. 모르는 내용은 빼고 confidence·inferenceLevel·uncertainty로 표현한다.
- 이 문장 규칙은 Event Agent의 정확한 사실 보고에는 적용하지 않는다.
- 병합·문장 수정이 끝난 뒤 길이와 duration을 검사하고, 반복마다 stale warning을 제거해 다시 계산한다.

### 질문

- 내부 모호성 질문과 event 회고 질문은 목적·저장 경계가 다르다.
- 회고 질문은 Repair 뒤 확정 event에만 붙고 Sleep/Wake/Movement에는 붙이지 않는다.
- 회고 질문은 event당 최대 하나, 하루 최대 5개, 물음표 종료, 255자 이하다.
- Question Agent 실패가 Timeline task 전체를 실패시키지 않는다.

### 결과·상태·오류

- 저장 전 event는 title, start, 유효 시간 범위, 하나 이상의 현재 task source를 가져야 한다.
- 0 event도 확정 결과로 result API에 보낸다.
- result 성공 뒤에만 SUCCESS callback을 보낸다. 그 뒤 callback 실패가 저장 결과를 FAILED로 바꾸지 않는다.
- User Memory 갱신은 callback이 없다. 어떤 실패 경로로 끝나도 결과 저장을 정확히 1회 호출한다.
- 실패 code는 API, callback, 운영 event에서 같은 정수를 쓰며 외부 문장은 카탈로그 안전 메시지만 쓴다.
- 401/404/409 App Server 거절은 retry와 callback 없이 abort한다.

### 보안·관측·운영

- taskToken·credential·presigned URL·사용자 본문은 허용되지 않은 로그·trace·result 경계로 보내지 않는다.
- 운영 이벤트는 allowlist field만 수집하며 일반 diagnostic log는 Elasticsearch에 적재하지 않는다.
- 관측 실패는 Timeline 처리를 실패시키지 않는다.
- 단일 worker와 프로세스 로컬 inflight를 유지해 `/ping` busy 의미와 배포 idle 대기를 보존한다.

## Invariants

이 문서의 `Current Implementation` 항목이 코드·prompt·테스트로 확인된 불변식이다. 자동 수정하지 않는 warning 규칙도 “문제를 숨기지 않는다”는 불변식으로 취급한다.

## Known Gaps

- Timeline trigger window의 역전은 endpoint에서 거절되지 않는다.
- 최종 문체의 1인칭·해요체·문장 수는 코드가 의미적으로 판정하지 않고 prompt와 live 품질 검증에 의존한다.
- App Server DB constraint와 callback/result idempotency는 이 저장소에서 확인할 수 없다.
- inbound 인증·인가가 없어 호출 주체 불변식은 코드로 강제되지 않는다.
- user memory 소비 경로는 input API 응답까지 열려 있으나, App Server가 실제로 값을 채워 보내는지는 이 저장소에서 확인할 수 없다.
- user memory 최대 1,000토큰 상한은 소비 측에서 강제하지 않는다. 소비 경로가 강제하는 것은 필드별 길이(200자)와 `customAttributes` 개수·길이(5개·150자)뿐이다.
- 생성 측(#64)은 1,000토큰을 **직렬화 1,200자**로 환산해 강제한다. 이 프로젝트에 tokenizer 의존성이 없고 provider가 셋이라 provider와 무관한 문자 수를 정본으로 삼은 것이며, 표준이 아니라 이 저장소가 고른 값이다.
- User Memory 갱신 입력 상한(하루 타임라인 5일·**하루당** event 20개·`memo` 500자)도 이 저장소가 고른 방어선이다. App Server 쪽 실제 상한과 일치하는지는 여기서 확인할 수 없다.

## Update When

validator, guard, prompt, schema, result/callback, security·observability test가 위 규칙을 추가·변경·삭제할 때 갱신한다. 구현 세부 리팩터링만으로는 갱신하지 않는다.

## Validation

- source·시간·결과: `uv run pytest tests/services -q`
- Agent·prompt·질문: `uv run pytest tests/agents tests/main -q -m "not live_llm"`
- 오류·보안·관측: `uv run pytest tests/core tests/api -q -m "not live_llm"`
- 특정 불변식의 이름·상수를 `rg`로 코드와 테스트 양쪽에서 확인

