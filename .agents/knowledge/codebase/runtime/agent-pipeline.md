# Agent pipeline

## Scope

정규화된 source가 event 후보, Timeline draft, 결정론 확정, 선택적 LLM Repair, 회고 질문으로 변환되는 순서와 각 단계의 책임을 설명한다.

## Read When

- Event/Timeline/Repair/Question Agent를 추가·수정할 때
- `draft_repair` guard 순서, Repair tool, fallback을 바꿀 때
- prompt 변경이 어느 단계의 의미 판단에 속하는지 판단할 때

## Authoritative Sources

- `app/agents/main/main_agent.py`, `app/agents/events/**`, `app/agents/timeline/timeline_agent.py`
- `app/agents/repair/repair_agent.py`, `app/agents/repair/tools.py`
- `app/agents/question/question_agent.py`와 prompt 세트
- `app/services/draft_repair.py`, `app/services/source_integrity.py`, `app/services/validator.py`, 각 `*_guard.py`
- `tests/main/**`, `tests/agents/**`, `tests/services/test_draft_repair.py`, guard별 테스트

## Current Implementation

### Main graph

1. `Location`, `Calendar`, `Photo`, `SleepActivity`, `Notification` Event Agent를 worker thread에서 병렬 실행한다.
2. Agent별 결과를 유일한 Agent 이름으로 보관하고 하나의 `AgentEventResult`로 취합한다. 취합 직후 candidate의 `places`/`address`를 `sourceRefs`로 찾은 입력에서 그대로 복사한다. `places`는 근거가 확실한 순서로 담긴 후보 목록이며 단수 필드를 두지 않는다 — 복수는 고를 후보(Timeline 입력), 단수는 고른 결과(`place`, Timeline 출력)다. Event Agent는 이 필드를 채우지 않는다 — 한 지점에 장소명이 여럿일 때 어느 것이 맞는지 판단할 근거가 없기 때문이며, 고르는 것은 User Memory를 가진 Timeline Agent다. 복사 지점이 fan-in인 이유는 Repair의 `rerun_timeline_agent`도 같은 함수를 지나기 때문이다.
3. Timeline Agent가 candidates와 fragments를 의미적으로 병합해 아직 확정되지 않은 draft를 만든다.
4. Repair Agent가 결정론 확정과 최대 `REPAIR_MAX_ITERATIONS`회의 LLM 개선을 수행한다.
5. Question Agent가 확정 event 모두에 회고 질문을 하나씩 붙인다.

Agent별 이름은 Repair의 `rerun_event_agent`가 특정 결과만 교체하는 key다. 이름이 중복되면 suffix를 붙이며, `event_agents`와 `event_results`는 같은 key를 유지해야 한다.

### Event Agent 경계

각 Event Agent 실패는 전체 pipeline을 중단하지 않고 빈 결과와 warning으로 흡수한다. 정상 결과도 다음 코드 검사를 거친다.

- 요청에 없는 rawId reference 제거, 유효한 근거가 사라진 candidate/fragment 제거
- 요청 window 밖 candidate/fragment 제거, 경계에 걸친 구간 clamp
- Agent에 전달된 source가 candidate 또는 fragment로 보존됐는지 coverage 검사

Event Agent는 정확한 source 사실과 수치·시각을 보고하는 계층이다. 최종 일기 문체 규칙을 이 단계에 강제하지 않는다.

### Timeline Agent 경계

Timeline Agent는 의미 병합과 tolerant parse를 맡는다. LLM 출력 계약은 `TimelineAgentOutput`(`events`·`warnings`)이며 내부 draft 전체가 아니다(#118). 개별 event/warning이 schema를 어기면 해당 항목만 제외하고 warning을 남긴다. 최상위 JSON을 읽지 못하거나 호출이 실패하면 빈 draft와 HIGH warning으로 fallback한다. `questions` 같은 다른 키는 무시한다.

LLM이 준 `userId`, date, timezone, `clientEventId`는 신뢰하지 않는다. date/timezone은 request 기준으로, event ID는 parse 순서로 임시 부여한다.

v3 Timeline 프롬프트는 판단 순서대로 읽힌다. 작업을 하루 구조 → 근거로 event 구성 → eventType·시간·장소 결정 → User Memory 반영 → 문장 → 최종 검증의 여섯 단계로 나누고, 3단계까지는 User Memory를 쓰지 않는다. 2·3단계의 공통 규칙은 기본값이고 타입별 절이 다르게 적으면 그 타입에서는 그 절이 이긴다. eventType마다 다른 Event Agent의 candidate에서 무엇을 보고 어떻게 합치는지(합치는 근거·보태는 근거·시간과 지속시간·장소·User Memory 구체화 범위·근거가 약할 때)와 candidate → event 예시는 그 타입의 절 하나에 모여 있다. 활동 근거 없이 체류만 있으면 고른 `place`의 성격으로 그곳에서 흔히 하는 일상 활동을 쓴다(#140, 「장소 성격으로 읽는 활동」 표: 대학교→`CLASS`, 회사·사무실 건물→`WORK`, 식당→`MEAL`, 관광지·번화가와 아파트·주택·빌라→`REST`). `INFERRED`·confidence 0.6 이하로 두고 과목·메뉴·업무 내용·동행은 지어내지 않으며, 다른 활동 근거가 있으면 그것이 이긴다. `REST`는 쉬었다는 근거(쉬는 장면 사진, User Memory 휴식 습관, 주거지·나들이 장소)가 있을 때만 쓰고, 활동 근거도 장소 성격도 없는 체류는 `UNKNOWN`이다. Repair v3 는 같은 표를 갖고 누락·불일치만 고친다(두 표가 같은지 테스트가 본다). 취소·변경 알림은 다루지 않는다. Event Agent가 이미 하는 판단(이동수단 라벨·경유지·예약 날짜·알림 가치·수면 유효성)은 Timeline에서 지웠다. v2는 그대로다. 타입 절의 시간은 "검토 기준 N시간"이고 자르는 상한이 아니다(#134). v3 Timeline은 #134에서 수면 금지를 걷어내고 `SLEEP` 절을 두었다 — 금지 때문에 Calendar의 수면 후보가 빠지고 코드가 그 일정을 일반 일정으로 되살려 같은 시간의 체류와 겹쳤다. 캘린더 수면은 계획이라 `INFERRED`로 두고 그 구간의 체류는 수면 event에 합친다. `WAKE_UP`은 그 절의 한 줄이다. v3 Question은 여전히 수면 예시가 없다.

### Repair Agent와 확정 pass

Repair는 시작할 때 LLM 호출 여부와 무관하게 `repair_draft`를 한 번 실행한다. 이후 반복은 `analyze → execute tools → confirm`이고, tool call이 없거나 `done`, 반복 상한에 도달하면 끝난다. LLM·parse 실패 시 마지막으로 확정된 deep copy로 되돌아가 warning을 추가한다. 개별 tool 실패는 tool result로 남아 다음 분석 입력이 된다. 순서는 언제나 코드 → LLM → 코드이고 마지막 단계는 코드 확정이다.

확정 pass는 **고치는 것과 찾는 것을 나눈다**(#119). 무엇을 고칠지가 규칙으로 정해져 있으면 코드가 고치고, 어디서 끊고 무엇을 남길지가 의미 판단이면 찾아서 Repair에 넘긴다. 이동 사이의 장시간 체류와 상한을 넘긴 event를 나누는 것은 뒤쪽이다. 그래서 Repair가 고치지 못하면(LLM 실패, 제한 시간, 반복 소진) 그 위반은 warning과 함께 그대로 저장된다. 코드가 강제하는 것은 사진 단일 귀속과 대화 event 하루 3개다. 대화는 기준이 알림 수 하나라 코드가 고를 수 있다.

체류 카드와 일정·사진 event(#138)도 이 나눔을 따른다. 겹치는 체류를 일정·사진 event 의 위치 근거로 붙이는 것은 규칙이라 코드가 하고, 따로 남은 체류 카드가 같은 방문을 그린 것인지(흡수)와 더 긴 다른 시간인지(근무 체류 안의 회의·점심, 둘 다 남김)는 의미 판단이라 Repair 가 `absorb_location_event` 로 정한다. 일정·사진 event 는 어떤 단계도 지우거나 합치지 않는다. 확정 pass 가 붙인 체류는 `reason` 표지로 가려지며 사진 단일 귀속은 그것을 그 event 의 근거로 치지 않는다 — 사진을 잃은 event 가 체류 카드로 살아남지 않게 한다.

현재 `repair_draft` 순서는 다음 의미 의존성을 가진다.

1. 환각 rawId 제거 → 실제 source type 정정
2. 누락 Calendar event 복원
3. duration 복원 → Location 근거 시간 정렬 → Meal duration → Sleep 경계
4. 요청 window 적용 → **위치 근거 연결**(#138, v3 세트: 일정·사진 event 에 같은 시간의 체류를 장소 근거로 붙임) → 장소 확정
5. 정렬 → 이동 없는 연속 STAY 병합 → 중복·겹침 정리
6. **Photo 단일 귀속 강제**(#119)
7. Calendar/STAY 장소 일치 confidence 보강
8. **수면과 겹친 event 제거**(#134, v3 세트): `SLEEP` event와 조금이라도 겹치는 다른 event를 자르지 않고 지운다. 지운 event의 사진·캘린더 근거는 수면 event로 옮긴다
9. **대화 event 개수 제한**(#119, v3 세트): 3개를 넘으면 알림이 많은 3개만 남긴다 → **문장의 입력 주소 원문 정리**(#138, v3 세트): 제목·본문에 입력 주소가 그대로 있으면 `place` 나 동·도로명(건물번호 제외)으로 바꾼다
10. 검사(고치지 않음): Photo·Notification 안전성, 최종 문장 길이, 지속시간, event 개수. event 개수는 v3 세트에서 10개, v1·v2 세트에서 24개로 잰다. v3 세트에서는 지속시간을 eventType별로 재고 이동 사이 장시간 체류, 일정·사진 event 와 겹치는 체류 카드(`LOCATION_ONLY_OVERLAP`), 제목의 `체류`·시각과 문장의 주소 모양을 더 본다
11. 재정렬 → `clientEventId` 부여. 번호는 여기서만 준다 — 중간 guard(window·수면)는 event 를 지워도 번호를 다시 매기지 않는다(#144)

1~9는 draft를 고치고 10은 고치지 않는다.

**Repair가 도는 동안 한 번 준 `clientEventId`는 바뀌지 않는다**(#144). Repair 작업 상태가 지금까지 준 번호의 장부(`issued_ids`)를 들고 확정마다 넘기며, 확정은 이미 준 번호를 유지하고 새 event(캘린더 복원·사진 event·split 조각·Timeline 재실행 결과)에만 장부의 가장 큰 번호 다음을 준다. 지운 번호는 비워 두고 다시 쓰지 않는다. 예전에는 확정마다 1번부터 다시 매겨, 앞 차례에서 지운 event만큼 뒤 번호가 당겨졌다. 도구 로그와 쌓인 보정 기록(`corrected`·`removed`·`added`)은 그때의 번호를 그대로 싣고, 모델이 그 번호로 부른 `update_event`가 다른 event를 고친 채 성공으로 끝났다. `rerun_timeline_agent`는 새 draft의 번호를 임시 id(`rerun-NNN`)로 바꿔 옛 번호를 다시 받지 않게 한다. 번호는 결과 저장 계약에 없는 AI 서버 안의 값이라 Repair가 끝난 뒤 `event-001`부터 다시 맞추지 않는다 — 같은 event는 Repair 로그부터 Question 단계까지 같은 번호이고, 번호는 시간순과 어긋날 수 있다. 저장 직전의 사진 단일 귀속도 새로 만든 event에만 번호를 준다. `verify_fragment_usage`는 이 확정 pass 뒤에 실행해 최종 event가 fragment-only 근거인지 검사한다. 반복마다 동일 warning을 dedupe한다.

단계마다 직전·직후의 event를 비교해 무엇이 바뀌었는지 기록한다(`confirm_report`, #119). guard는 고치지 않고 옆에서 적는다. 어느 event의 어느 값이 무엇에서 무엇으로 바뀌었는지, 지워지거나 합쳐진 event의 전체 내용, 코드가 찾았지만 고치지 않은 것을 담는다. 기록은 draft가 아니라 Repair의 작업 상태가 들고 있어 결과 저장 계약에 나가지 않는다.

main agent는 draft를 돌려주기 직전에 사진 단일 귀속을 한 번 더 강제한다. 확정 뒤에 draft를 만지는 단계가 사진 참조를 어긋나게 하더라도 어긋난 채로 저장되지 않게 하는 마지막 자리다. 저장을 실패시키지 않고 바로잡는다.

### Repair 입력과 도구

Repair 프롬프트는 반복마다 그 시점의 draft로 새로 만든다. v3 세트에서는 `[자동 검사 결과]`(코드가 고친 것·지운 것·찾은 것), `[event 근거]`(event가 참조한 rawId의 candidate·fragment), `[user memory]`를 함께 싣는다(#119). 찾은 것은 확정할 때마다 그 draft로 다시 계산한 값만 싣고, 고친 것은 몇 번째 확정에서 나온 것인지 붙여 쌓는다. candidate 본문은 한 번만 싣고 event는 id로 가리키며, 어느 event에도 쓰이지 않은 candidate는 한 줄 요약만 싣는다. candidate 본문에는 장소 후보 `places`와 `address`도 싣는다(#140) — Repair가 event의 `place`를 다른 후보로 바꿀 재료이고, `[근거 원본]`(v1·v2 공용)은 단수 `place`만 보인다.

#119의 Repair 계약은 v3 세트에서만 돈다. 확정 pass의 새 검사, Repair의 새 입력, `split_event` 도구가 한 묶음이고 갈리는 기준은 `prompt_loader.uses_legacy_contract` 하나다. v1·v2 세트에서 Repair가 보는 warning·입력·도구는 예전 그대로다. v2 프롬프트는 이것들을 설명하지 않고 v2는 운영 세트라 고치지 않는다. 실제 LLM으로 확인한 것이다 — `split_event`를 v2에 내놓자 캘린더 일정대로인 event와 사진 event를 잘게 쪼개 event가 7개에서 13개로 늘었고, 새 검사의 warning을 보이자 나눌 도구가 없는 v2가 Timeline 재실행을 두 번 불렀으며 위반은 그대로 남았다. draft를 **고치는** 단계는 세트와 무관하게 같다 — 사진 단일 귀속은 어느 세트에서든 강제한다.

`split_event`는 조각의 시간·타입·문장을 Repair에게 받고 **근거는 코드가 원본 시각을 보고 나눠 담는다.** 시점 근거(사진·알림)는 그 시각을 포함하는 조각 하나에만, 구간 근거(체류·이동·일정)는 겹친 길이가 근거나 조각의 절반 이상인 조각 모두에 담는다. 원래 event의 근거는 하나도 버리지 않는다. 원래 event의 시간 밖으로 걸친 조각은 거절하지 않고 안쪽만 남기며, 통째로 밖에 있는 조각은 뺀다. 원래 event의 구간 근거 중 어느 조각과도 겹치지 않는 것이 있으면 거절한다 — 조각이 덮지 않은 시간의 체류나 이동이 조용히 사라지지 않게 한다.

이동 사이 장시간 체류의 검사 결과는 걸린 체류(`longStays`)와 나눌 자리(`segments`)를 함께 준다. 나눌 자리는 event가 근거로 댄 이동과 체류를 시간순으로 놓고 이어진 이동과 20분 이하 체류를 하나의 이동으로 묶은 것이며, **event의 시간 안으로 맞춘 값**이다. 요청 시간 범위 끝에서 잘린 event는 근거가 그 뒤까지 이어져 있어, 근거 원본의 시간을 그대로 주면 Repair가 event 밖으로 나가는 조각을 만든다. 하나의 이동으로 묶는 것은 **수집이 끊기지 않고 이어진 근거끼리**다(`location_metrics.COVERAGE_GAP_MIN`). 한 event가 하루의 위치 기록을 거의 다 근거로 대면, 시간이 이어지는지 보지 않고 묶었을 때 오전의 몇 분짜리 체류와 밤의 이동이 11시간짜리 이동 하나가 된다. 이동과 이어지지 않은 20분 이하 체류는 나눌 자리에 넣지 않고 `shortStays`로 따로 알린다 — 그것으로 조각을 만들면 몇 분짜리 체류 카드가 생긴다.

지속시간 검사의 값은 **자르는 상한이 아니라 검토 기준**이다(#134). 기준을 넘은 event를 짚어 Repair가 그 안에 묻힌 사건이 있는지 먼저 보게 할 뿐 나누라는 뜻이 아니고, warning 문장도 나누라고 말하지 않는다. #119에서는 넘으면 나누라고 알렸고, 실제 LLM이 경계 없는 9.5시간 체류를 같은 문장의 `REST` 조각 다섯 개로 고르게 나눠 event가 5개에서 12개로 늘었다. 나누는 기준은 길이가 아니라 근거다 — Repair는 `findings`가 가리키지 않은 event라도 독립 사건(식사·회의·수업·별도 방문)이 흡수됐다는 구체적 근거가 있으면 되살려 나누고, 몇 분짜리 체류 기록의 경계·20분 이하 체류·지점 이름 차이로는 나누지 않는다. 이동 없이 이어진 체류는 길이와 장소명에 무관하게 하나지만, **이동은 예외 없이 나눈다** — 같은 캠퍼스 안이어도 이동 사이에 20분을 넘는 체류가 있으면 나눈다. event 10개 제한과 연속 체류 보존이 길이를 맞추는 분할보다 먼저이고, 근거 없이 나뉜 조각은 `update_event`+`delete_event`로 되합친다(일반 병합 도구는 없다. `absorb_location_event` 는 체류 카드를 일정·사진 event 에 흡수하는 데만 쓴다).

지속시간 검사는 **Repair가 고칠 수 없는 것을 알리지 않는다.** 근거가 전부 한 묶음(이동 없이 같은 장소에서 이어진 STAY)의 체류인 event는 확정 pass가 하나로 합치므로(`merge_stay_events`) 나눠도 다음 확정에서 다시 합쳐진다. 실제 LLM은 3.4시간짜리 체류를 세 번 나눴고 세 번 다 도로 합쳐져 반복을 모두 썼다. 그런 event는 상한 검사에서 면제한다.

v3 Repair 프롬프트는 코드가 이미 본 것을 다시 검증하지 않는다. 작업 순서는 코드가 찾은 것 해소 → 코드가 고친 event 다시 쓰기 → 내용이 부족한 event 구체화 → 문장 다듬기다. candidate·fragment에 글자 그대로 없어도 합리적으로 추론되는 사람·장소·활동·목적을 허용하고 `INFERRED`로 둔다. Timeline의 추론을 되돌리지 않게 하는 규칙이 함께 있다 — Timeline이 쓴 구체적인 이름을 넓은 말로 뭉개지 않고, warning을 "해소"와 "검토만"으로 나눠 읽고, 이동 분할이 합치라는 warning보다 먼저이고, 재실행은 마지막 수단이다. Repair는 v2처럼 작업 전에 "오늘 어떤 하루였나"를 한 문장으로 정리한다. 생활 장소명(집 등)은 User Memory를 참고하되 하루의 흐름으로 직접 판단하고, 집을 붙이라는 규칙은 두지 않는다(#134). 집 규칙을 두는 방식도 live로 시험했지만 실제 trace 입력에서는 판단을 맡긴 쪽이 v2와 같은 자연스러운 표현(문장에서 `집`, `place`는 원래 장소명)을 냈다. 코드가 되살린 수면 일정은 `SLEEP`으로 고쳐 쓰고 같은 시간의 체류 event와 합친다.

### Question Agent

회고 질문은 Repair가 event를 삭제·병합하고 ID를 확정한 뒤 생성한다. 모든 event가 대상이며 종류에 따른 예외는 없다. LLM에는 event의 ID, 종류, title, 시간대, 선택적 description/place만 주며 confidence, inference level, uncertainty, sourceRef는 주지 않는다.

질문은 물음표로 끝나야 하고 255자 이하여야 하며 event당 첫 질문 하나만 적용한다. 모르는 event ID와 중복은 제외하고, 1차에서 빠진 event는 한 번 더 묻는다. Question Agent 실패는 warning을 남기고 질문 없는 draft로 저장을 계속한다.

v3 Question 프롬프트는 수면을 뺀 eventType마다 예시를 두고, 한 질문에 두 가지를 이어 묻는 것을 허용하며, 무엇을 했는지가 빠진 event는 그것을 먼저 묻는다(#118). 내부 모호성 질문(`TimelineDraft.questions`)은 #118에서 제거됐다.

## Invariants

- Event Agent 병렬 실행 후 명시적인 merge를 거쳐야 Timeline Agent로 간다.
- 위경도는 프롬프트에 싣지 않는다. request로는 계속 받고 코드가 파생 지표 계산에만 쓴다. 장소 확정 우선순위는 `STAY → MOVEMENT → PHOTO → CALENDAR`이며, PHOTO의 장소는 들어오지 않을 수 있다.
- 장소 문자열은 코드가 입력에서 복사하고 LLM은 고르기만 한다. candidate 단계에서 복사하고, 확정 pass에서 draft의 `place`가 입력 또는 User Memory에서 왔는지 검사한다. 근거 없는 `place`는 warning만 남기고 지우지 않으며, 근거 없는 `address`는 지운다.
- rawId 무결성과 request window는 candidate와 final draft 양쪽에서 방어한다.
- Calendar 누락 방지, 정렬, ID, source/시간 확정은 LLM 선택에 의존하지 않는다.
- 병합으로 event 구성이 바뀐 뒤에 Photo/Notification/길이 검사를 수행한다.
- 길이·duration·event 개수·이동 사이 체류 guard는 반복마다 자기 이전 warning을 제거하고 현재 draft를 다시 잰다.
- 프롬프트 세트가 설명하지 않는 warning·입력·도구를 코드가 먼저 주지 않는다. 새 검사를 더할 때는 그것을 읽고 고칠 수 있는 세트에서만 돌린다.
- 입력의 모든 사진은 발행되는 모든 확정본과 main agent가 돌려주는 draft에서 정확히 한 event에만 있다. LLM 호출 여부와 무관하다.
- 이동 사이 체류와 긴 event에 묻힌 사건을 나누는 것은 코드가 하지 않는다. 코드는 찾기만 한다.
- 대화 event는 v3 세트에서 확정할 때마다 3개 이하로 맞춘다. 기준은 알림 수이고 같으면 먼저 시작한 대화를 남긴다.
- 카탈로그에 싣는 도구와 실행을 허용하는 도구는 같다. 세트가 주지 않는 도구는 불러도 실행하지 않는다.
- Question Agent는 Repair 뒤, 결과 저장 앞이다.

## Known Gaps

- 일부 코드 주석은 제거된 `timeline_items`·향후 N:M DB 구조를 언급하지만 현재 앱에는 해당 persistence가 없다.
- Timeline Agent는 `userId`를 고정 placeholder로 만든다. App Server 결과 저장 계약에는 userId가 없어 밖으로 전송되지는 않는다.
- LLM 결과 품질은 opt-in live test 외에 결정론적으로 보장되지 않는다. 기본 테스트는 FakeLLM과 guard 계약을 검증한다.
- 마지막 단계가 항상 코드 확정이라, Repair의 마지막 수정이 guard에 걸려 다시 보정되면 그 뒤에는 문장을 다듬을 LLM 차례가 없다.
- 기존 `location_guard`의 장거리 여정 검사는 사이의 체류 길이를 보지 않는다. 20분을 넘는 체류를 사이에 둔 장거리 이동을 나누면 "하나의 여정으로 묶은 후보가 없다"는 warning이 남는다. v3 Repair 프롬프트가 그 warning을 따르지 않게 한다.
- 수면 기록(HEALTH)이 입력에 계속 들어오는지는 이 저장소에서 확인할 수 없다.

## Update When

main graph node·순서·병렬성, Agent fallback, Event 결과 방어, Repair 반복·tool·confirm 순서, Question 의미·제약이 달라질 때 갱신한다.

## Validation

- `uv run pytest tests/main tests/agents/test_repair_agent.py tests/agents/test_repair_inputs.py tests/agents/test_repair_invariants.py tests/agents/test_timeline_json_validation.py -q`
- `uv run pytest tests/services/test_draft_repair.py tests/services/test_confirm_report.py tests/services/test_draft_edit.py tests/services/test_source_integrity.py tests/services/test_fragment_guard.py -q`
- guard 변경 시 해당 `tests/services/test_*_guard.py` 실행
- `rg -n "add_node|add_edge|repair_draft\(|verify_.*\(|renumber_events|QuestionAgent" app/agents app/services`
