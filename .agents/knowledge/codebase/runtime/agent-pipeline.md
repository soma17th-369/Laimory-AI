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
2. Agent별 결과를 유일한 Agent 이름으로 보관하고 하나의 `AgentEventResult`로 취합한다.
3. Timeline Agent가 candidates와 fragments를 의미적으로 병합해 아직 확정되지 않은 draft를 만든다.
4. Repair Agent가 결정론 확정과 최대 `REPAIR_MAX_ITERATIONS`회의 LLM 개선을 수행한다.
5. Question Agent가 확정 event 중 질문할 가치가 있는 최대 5개에 회고 질문을 붙인다.

Agent별 이름은 Repair의 `rerun_event_agent`가 특정 결과만 교체하는 key다. 이름이 중복되면 suffix를 붙이며, `event_agents`와 `event_results`는 같은 key를 유지해야 한다.

### Event Agent 경계

각 Event Agent 실패는 전체 pipeline을 중단하지 않고 빈 결과와 warning으로 흡수한다. 정상 결과도 다음 코드 검사를 거친다.

- 요청에 없는 rawId reference 제거, 유효한 근거가 사라진 candidate/fragment 제거
- 요청 window 밖 candidate/fragment 제거, 경계에 걸친 구간 clamp
- Agent에 전달된 source가 candidate 또는 fragment로 보존됐는지 coverage 검사

Event Agent는 정확한 source 사실과 수치·시각을 보고하는 계층이다. 최종 일기 문체 규칙을 이 단계에 강제하지 않는다.

### Timeline Agent 경계

Timeline Agent는 의미 병합과 tolerant parse를 맡는다. LLM payload의 개별 event/question이 schema를 어기면 해당 항목만 제외하고 warning을 남긴다. 최상위 JSON을 읽지 못하거나 호출이 실패하면 빈 draft와 HIGH warning으로 fallback한다.

LLM이 준 `userId`, date, timezone, `clientEventId`는 신뢰하지 않는다. date/timezone은 request 기준으로, event ID는 parse 순서로 임시 부여한다.

### Repair Agent와 확정 pass

Repair는 시작할 때 LLM 호출 여부와 무관하게 `repair_draft`를 한 번 실행한다. 이후 반복은 `analyze → execute tools → confirm`이고, tool call이 없거나 `done`, 반복 상한에 도달하면 끝난다. LLM·parse 실패 시 마지막으로 확정된 deep copy로 되돌아가 warning을 추가한다. 개별 tool 실패는 tool result로 남아 다음 분석 입력이 된다.

현재 `repair_draft` 순서는 다음 의미 의존성을 가진다.

1. 환각 rawId 제거 → 실제 source type 정정
2. 누락 Calendar event 복원
3. duration 복원 → Location 근거 시간 정렬 → Meal duration → Sleep 경계
4. 요청 window 적용 → 장소 확정
5. 정렬 → 이동 없는 연속 STAY 병합 → 중복·겹침 정리
6. 병합 후 Photo 단일 귀속과 Notification 안전성 검사
7. Calendar/STAY 장소 일치 confidence 보강
8. 최종 문장 길이와 장시간 event 검사
9. 재정렬 → `clientEventId` 재부여 → 내부 모호성 질문 문장 보정

`verify_fragment_usage`는 이 확정 pass 뒤에 실행해 최종 event가 fragment-only 근거인지 검사한다. 반복마다 동일 warning을 dedupe한다.

### Question Agent

회고 질문은 Repair가 event를 삭제·병합하고 ID를 확정한 뒤 생성한다. `SLEEP`, `WAKE_UP`, `MOVEMENT`는 질문 대상에서 제외한다. LLM에는 event의 ID, 종류, title, 시간대, 선택적 description/place만 주며 confidence, inference level, uncertainty, sourceRef는 주지 않는다.

질문은 물음표로 끝나야 하고 255자 이하여야 하며 event당 첫 질문 하나만 적용한다. 모르는 event ID, 중복, 초과 질문은 제외한다. Question Agent 실패는 warning을 남기고 질문 없는 draft로 저장을 계속한다.

`TimelineDraft.questions`는 시간·장소 모호성을 확인하는 내부 질문이고 `TimelineEventDraft.question`은 사용자에게 저장되는 회고 질문이다. 서로 대체하지 않는다.

## Invariants

- Event Agent 병렬 실행 후 명시적인 merge를 거쳐야 Timeline Agent로 간다.
- rawId 무결성과 request window는 candidate와 final draft 양쪽에서 방어한다.
- Calendar 누락 방지, 정렬, ID, source/시간 확정은 LLM 선택에 의존하지 않는다.
- 병합으로 event 구성이 바뀐 뒤에 Photo/Notification/길이 검사를 수행한다.
- 길이·duration guard는 반복마다 자기 이전 warning을 제거하고 현재 draft를 다시 잰다.
- Question Agent는 Repair 뒤, 결과 저장 앞이다.

## Known Gaps

- 일부 코드 주석은 제거된 `timeline_items`·향후 N:M DB 구조를 언급하지만 현재 앱에는 해당 persistence가 없다.
- Timeline Agent는 `userId`를 고정 placeholder로 만든다. App Server 결과 저장 계약에는 userId가 없어 밖으로 전송되지는 않는다.
- LLM 결과 품질은 opt-in live test 외에 결정론적으로 보장되지 않는다. 기본 테스트는 FakeLLM과 guard 계약을 검증한다.

## Update When

main graph node·순서·병렬성, Agent fallback, Event 결과 방어, Repair 반복·tool·confirm 순서, Question 의미·제약이 달라질 때 갱신한다.

## Validation

- `uv run pytest tests/main tests/agents/test_repair_agent.py tests/agents/test_timeline_json_validation.py -q`
- `uv run pytest tests/services/test_draft_repair.py tests/services/test_source_integrity.py tests/services/test_fragment_guard.py -q`
- guard 변경 시 해당 `tests/services/test_*_guard.py` 실행
- `rg -n "add_node|add_edge|repair_draft\(|verify_.*\(|renumber_events|QuestionAgent" app/agents app/services`
