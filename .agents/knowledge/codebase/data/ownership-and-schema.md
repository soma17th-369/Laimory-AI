# 데이터 소유권·스키마 경계

## Scope

제품 persistence, cache, source·draft·result 스키마의 소유권과 변환 규칙을 설명한다. 클래스와 필드 전체 목록은 복제하지 않는다.

## Read When

- DB·cache·저장 경로를 추가하거나 바꿀 때
- source type, rawId, 내부 draft 또는 result schema를 변경할 때
- normalizer·validator·mapper 책임을 이동할 때

## Authoritative Sources

- `pyproject.toml`, `app/core/config.py`
- `app/schemas/common.py`, `source_snapshot.py`, domain schema, `event_candidate.py`, `timeline_request.py`, `timeline.py`, `timeline_input.py`, `timeline_result.py`, `task.py`, `user_memory.py`, `user_memory_update.py`
- `app/services/normalizer.py`, `source_contract.py`, `source_integrity.py`, `timeline_validator.py`, `timeline_result.py`, `user_memory_limits.py`, `user_memory_repair.py`
- `app/services/app_server_client.py`, `app/services/timeline_runner.py`, `app/services/user_memory_runner.py`
- `tests/services/test_normalizer.py`, `test_source_contract.py`, `test_source_integrity.py`, `test_timeline_result.py`, `test_timeline_validator.py`, `test_user_memory_limits.py`, `test_user_memory_repair.py`

## Current Implementation

### Persistence와 cache

AI 서버에는 제품 DB driver, ORM, repository, migration, DB 접속 설정이 없다. source 조회와 Timeline 결과 persistence는 App Server HTTP API가 소유한다. task 상태도 App Server가 소유한다. `data/input`·`data/output`은 live LLM fixture와 비교 산출물용 로컬 파일이며 운영 저장소가 아니다.

Redis나 제품 cache도 없다. `lru_cache`로 유지되는 Settings, provider/client, notification dictionary와 module-level prompt는 프로세스 최적화일 뿐 제품 데이터의 권위 원천이 아니다. inflight counter 역시 순간 health gauge다.

애플리케이션은 Elasticsearch에 직접 저장하지 않는다. 운영 로그 persistence는 stdout을 읽는 별도 Filebeat와 Elasticsearch가 소유하며, AI 실행 trace는 선택적 Langfuse가 소유한다.

### 데이터 단계

| 단계 | 역할과 소유권 |
|---|---|
| Timeline trigger | task 상관값, 최초 token, dailyRecordId, 정본 window만 전달 |
| `TimelineInputPayload` | 입력 한 벌(`taskId` 포함) field 선언의 유일한 자리(#102). 입력 조회 응답과 동기 테스트 요청이 함께 상속한다 |
| `TimelineInputResponse` | `TimelineInputPayload` + `taskToken`. App Server input 전송 계약 |
| `CollectedSnapshot` | 평평한 `sourceItems`를 보관하는 pipeline 경계 계약 |
| `TimelineDraftRequest` | normalizer가 source를 domain list로 분리한 Agent 입력 |
| `AgentEventResult` | Event Agent의 candidate·fragment·warning 중간 계약 |
| `TimelineDraft` | Timeline/Repair/Question이 다루는 넓은 내부 draft |
| `TimelineResultRequest` | App Server로 보내는 좁은 persistence 계약 |
| `TimelineCallbackPayload` | 결과가 아닌 terminal 상태 통보 계약 |
| `UserMemoryUpdateRequest` | User Memory 갱신 접수 계약(#64). 확정된 `dailyTimelines`와 기존 profile |
| `DailyTimelineDigest` | prompt에 실을 만큼으로 줄인 하루 타임라인과 잘라낸 양 |
| `UserMemoryResultRequest` | 갱신본 저장과 종료 통보를 겸하는 계약 |

`CamelModel`은 JSON alias는 camelCase, Python construction은 snake_case도 허용한다. `rawId`는 UUID로 검증하고 표준 문자열로 정규화한다. source 식별자는 rawId 하나이며 내부 DB ID fallback은 없다.

입력 source는 `STAY`, `MOVEMENT`, `CALENDAR`, `HEALTH`, `NOTIFICATION`, `PHOTO`이고 HEALTH는 현재 `STEPS`, `SLEEP` metric을 구분한다. snapshot payload는 처음에는 dict로 받고 normalizer가 item type별 domain model로 검증한다. 개별 item 검증 실패는 code와 item type/rawId를 로컬 진단에 남기고 해당 item만 건너뛴다.

source batch는 taskId 일치, 1건 이상, rawId 유일성을 요구한다. Event candidate와 final draft는 입력에 없는 rawId를 제거하고 유효한 근거가 하나도 남지 않은 항목을 제외한다. 저장 직전에는 title, start, time order, non-empty source, rawId task 소속을 다시 검증한다.

result mapper는 내부 판단 필드를 버리고 사람이 읽는 event와 sourceRawIds만 전송한다. description은 subtitle이 되고, rawId는 reference 순서를 보존해 dedupe한다. date/time은 draft timezone으로 localize한다. event가 0개여도 확정 결과로 전송한다.

Photo의 `photoUrl`은 image fetch에만 사용하며 Pydantic serialization에서 제외된다. presigned query가 prompt·trace로 유출되지 않도록 하기 위한 데이터 경계다. client URI와 filename은 schema에 없어 무시된다.

User Memory는 App Server가 소유하고 AI 서버는 읽기(input 조회)와 쓰기(갱신 결과 저장) 둘 다 HTTP로만 한다. 갱신은 append가 아니라 **전체 rewrite**이며, 출력이 기존 값을 통째로 대체한다. `schemaVersion`과 `updatedAt`은 LLM 값이 아니라 서버가 박는다 — 모델이 정하게 두면 언젠가 우리가 모르는 버전이 저장되고 다음 날 읽기가 깨진다.

갱신 입력의 `title`·`subtitle`·`question`은 **이 시스템의 Timeline·Question Agent가 쓴 문장**이고, 사용자가 직접 쓴 글은 `memo` 뿐이다. 이 출처 구분이 계약 수준의 의미를 갖는다 — AI가 쓴 문장에서 성향을 뽑으면 모델이 자기 출력을 읽고 사용자를 만들어 내는 되먹임이 되고, 그 profile이 다시 다음 Timeline 문장을 만드는 데 쓰여 스스로를 강화한다. 성향 계열 다섯 필드(`personality`, `values`, `preferences`, `emotionalPatterns`, `memoryStyle`)의 근거는 `memo` 뿐이며, `memo`가 없는 날은 그 필드가 그대로인 것이 정상이고 결과는 `SUCCESS`다.

## Invariants

- App Server가 제품 persistence와 task 상태의 유일한 소유자다.
- AI 서버에 DB 직접 접근 경로를 되살리지 않는다.
- rawId는 UUID이며 한 task 입력에서 유일하고 모든 결과 source가 그 입력 집합에 속한다.
- 내부 draft와 outbound persistence schema를 같은 모델로 합치지 않는다.
- result가 0건이어도 저장 request를 생략하지 않는다.
- URL·token·내부 파일 식별자를 LLM 입력이나 저장 결과에 섞지 않는다.
- source 하나가 여러 event의 근거가 되는 것은 허용한다.
- User Memory의 `schemaVersion`·`updatedAt`은 서버가 확정한다. LLM 출력값을 그대로 저장하지 않는다.
- 성향 계열 필드는 사용자가 직접 쓴 `memo`만 근거로 한다. AI가 쓴 문장에서 사용자 특성을 만들지 않는다.
- 크기 상한을 넘은 갱신본은 잘라서 저장하지 않는다. 다시 요청하고, 소진하면 저장하지 않는다.

## Known Gaps

- App Server의 실제 DB table, column, transaction, retention, unique constraint는 이 저장소에 없다. 255자 절단은 mapper 코드와 기존 계약에 근거하지만 DB DDL로 직접 검증할 수 없다.
- `TimelineInputResponse.userMemory`는 선택 필드다. App Server가 실제로 값을 채우는지는 이 저장소에서 확인할 수 없고, 없으면 `CollectedSnapshot.userMemory`가 `None`이다.
- source의 시각은 boundary schema에서 문자열로 유지하고 여러 형식을 관대하게 parse한다. 모든 source timestamp가 schema 단계에서 timezone-aware임을 강제하지 않는다.
- `UserMemory`는 고정 schema v1.0이다(#65). 자유도는 `customAttributes`(최대 5개, 값당 150자) 안에만 있고, 최상위는 `extra="forbid"`다. AI가 만드는 `customAttributes` 키에 결정론 코드가 의존하지 않는다.
- 코드 일부에 과거 DB table·향후 N:M 연결을 설명하는 stale 주석이 남아 있으나 현재 구현 계약은 아니다.

## Update When

제품 데이터·task·관측 저장 소유자, DB/cache 존재 여부, 데이터 단계, source 종류, rawId 규칙, normalizer 실패 정책, draft→result narrowing, 저장 전 검증이 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/services/test_normalizer.py tests/services/test_source_contract.py tests/services/test_source_integrity.py -q`
- `uv run pytest tests/services/test_timeline_result.py tests/services/test_timeline_validator.py -q`
- `uv run pytest tests/core/test_no_direct_elasticsearch.py -q`
- `rg -n -i "sqlalchemy|redis|database_url|create_engine" app pyproject.toml`
- `rg -n "RawId|source_raw_ids|build_result_request|ensure_timeline_valid_for_storage" app`

