# 관측성·오류 추적

## Scope

운영 이벤트, 로컬 진단, Filebeat/Elasticsearch, Langfuse trace, 오류 코드와 redaction 경계를 설명한다.

## Read When

- 로그·metric·trace·ErrorCode를 추가하거나 바꿀 때
- 사용자 본문·secret이 관측으로 나갈 가능성이 있을 때
- HTTP/task/dependency 실패를 운영에서 추적할 때

## Authoritative Sources

- `app/core/logging.py`, `operational_logging.py`, `langfuse_tracing.py`, `redaction.py`, `error_codes.py`, `exceptions.py`, `execution_context.py`
- `app/api/request_logging.py`, `app/api/error_handlers.py`
- `app/services/timeline_runner.py`, `app/services/user_memory_runner.py`, `app/services/app_server_client.py`
- `docs/observability/filebeat.example.yml`, `scripts/deploy-ec2.sh`
- `tests/core/test_logging.py`, `test_operational_logging.py`, `test_langfuse_tracing.py`, `test_redaction.py`, `test_no_direct_elasticsearch.py`

## Current Implementation

관측은 세 경계로 나뉜다.

| 경계 | 목적 | persistence |
|---|---|---|
| 일반/local 진단 로그 | exception 원문, stage 디버깅 | container stdout; Filebeat 표식이 없어 ES에서 drop |
| 운영 이벤트 | HTTP/server/task/dependency 집계 | stdout 한 줄 JSON → Filebeat → Elasticsearch data stream |
| Langfuse | Agent tree, LLM generation, token, 선택적 본문 | Langfuse SDK; 비활성·오류 시 no-op |

`LOG_FORMAT=rich`는 local console, `json`은 운영용 한 줄 JSON이다. JSON formatter는 multiline traceback을 한 record의 구조화 필드로 넣고 secret/PII를 마스킹한다. Uvicorn logger는 앱 formatter에 맞추고 기본 access log는 요청 middleware가 대체한다.

Elasticsearch 수집 대상은 `emit_event`만 붙일 수 있는 `event.dataset=laimory.api` 표식으로 제한한다. 현재 event action은 HTTP request 완료, server 시작·종료, Timeline task 완료, User Memory task 완료(`usermemory.task.completed`), App Server logical request 완료·retry다. 이벤트별 field allowlist가 있으며 allowlist 밖 값은 값 자체를 기록하지 않고 field name만 local DEBUG로 알린다.

HTTP request는 response start 시점에 요청당 한 건, Timeline·User Memory background task는 task당 한 건, App Server logical call은 retry 횟수와 무관하게 완료 한 건을 남긴다. 개별 retry는 별도 retry event다. 같은 `taskId`와 `errorCode`로 경계를 연결한다.

`usermemory.task.completed`에서 먼저 볼 field는 `resultSent`다. callback이 없는 계약이라 결과 저장 호출 한 번이 통보의 전부이고, `false`면 App Server는 아무 연락도 받지 못한 상태다. `droppedDailyTimelineCount`/`droppedEventCount`는 입력을 얼마나 잘랐는지를 남긴다 — 조용히 자르면 결과만 보고 "다 보고 이 정도"인지 "못 본 게 있어서 이 정도"인지 구분할 수 없다.

`ErrorCode`는 영역별 정수 대역, 외부 안전 메시지, 필요한 HTTP status의 단일 카탈로그다. 예약된 과거 번호는 재사용하지 않는다. `report_error`는 최종/흡수 경계의 local 진단이며 외부 response·callback·운영 event는 같은 code를 참조한다. 원본 exception message와 traceback은 외부 안전 메시지로 변환된다.

Langfuse는 설정이 활성이고 public/secret key가 모두 있을 때 지연 생성한다. release는 `AGENT_VERSION`, environment는 `APP_ENV`를 쓴다.

Timeline과 User Memory 갱신은 **별개 trace**다. 화면에서 두 작업을 가르는 값은 셋이다.

| 값 | Timeline | User Memory |
|---|---|---|
| trace name | `generate-timeline` | `update-user-memory` |
| tag | `timeline` | `user-memory` |
| metadata `feature` | `timeline` | `user-memory` |

LLM generation 이름도 실행 단계로 갈린다 — `infer-{agent}-events`, `generate-timeline-draft`, `analyze-timeline-repair`, `generate-event-questions`, `update-user-memory-profile`, `describe-photo-images`. 이름이 `call-llm`으로 퇴화하면 두 작업의 지연·토큰이 화면에서 한 덩어리로 보여 어느 쪽이 느려졌는지 알 수 없다. **새 `ExecutionStage`를 추가하면 `_trace_generation`의 이름 분기도 함께 늘린다.** 이 대응은 `tests/core/test_langfuse_tracing.py`가 고정한다.

`session_id`는 두 작업 모두 `taskId`다. 서로 다른 작업이라 값이 겹치지 않지만, App Server가 taskId를 재사용하면 두 흐름의 trace가 한 session으로 합쳐진다. content policy는 명시 설정이 우선하며 미지정 시 local/dev는 `SANITIZED`, 그 밖은 `NONE`이다. `NONE`도 duration, token, count, errorCode 같은 diagnostics는 보존하고 사용자 본문은 byte/hash summary로 접는다. payload size를 제한하고 export 직전 OTel attribute를 다시 마스킹한다.

`userMemory` 키의 값은 정책과 무관하게 `redact_value`가 비식별 요약(`schemaVersion`, 채워진 필드 수, `customAttributeCount`, byte/hash)으로 바꾼다(#65). `dailyTimelines` 키도 같은 이유로 개수 요약(`dailyTimelineCount`, `eventCount`, `memoCount`, byte/hash)으로 바꾼다(#64) — `memo`는 사용자가 직접 쓴 글이고 `title`/`subtitle`은 사용자가 읽는 문장이다. 두 요약 모두 필드 이름 목록에 의존하지 않아 App Server가 필드를 더해도 본문이 새지 않는다. 사용자 profile 문장은 마스킹 pattern에 걸릴 형태가 아니므로 key 이름으로 접는다. 이 치환이 log·Langfuse input/output·metadata의 공용 경로에 있어, 호출부가 snapshot이나 정규화 request를 통째로 dump해도 본문이 나가지 않는다. prompt 본문(generation input)에는 값이 들어가며 그것은 content policy가 통제한다.

Langfuse client 생성·span 시작/종료·flush 실패와 운영 event 조립·handler 실패는 주 요청을 실패시키지 않는다. 앱 코드의 Elasticsearch 직접 호출과 알려지지 않은 `httpx` 사용자는 정적 테스트가 막는다.

## Invariants

- 일반 로그를 추가해도 자동으로 Elasticsearch 수집 범위가 넓어지지 않아야 한다.
- 운영 이벤트에는 사용자 title, 장소, 주소, filename, prompt/response, URL, token, exception 원문을 넣지 않는다.
- event action 이름과 field 의미는 dashboard/search 계약이므로 임의 변경하지 않는다.
- 같은 실패는 API/callback/운영 event에서 같은 정수 code를 쓴다.
- 관측 실패가 Timeline 결과를 바꾸지 않는다.
- `taskToken` 값은 Langfuse와 모든 로그에서 금지하고 갱신 횟수만 허용한다.
- user memory 본문과 하루 타임라인 본문(`memo`·`title`·`subtitle`)은 로그·관측 어디에도 남기지 않고 개수·크기 요약만 남긴다. 계약 위반을 기록할 때도 pydantic 오류 문자열을 그대로 넘기지 않는다(걸린 값을 인용한다).
- 크기·민감정보 지적 문장은 prompt와 로그에 그대로 실리므로 걸린 값을 인용하지 않는다. 필드 이름과 pattern label까지다.

## Known Gaps

- Elasticsearch index template·dashboard·retention 설정은 저장소에 없고 배포 환경이 소유한다.
- Filebeat가 실패해도 앱 배포는 계속되므로 운영 이벤트 전달 보장은 없다.
- Langfuse는 optional이고 sampling이 1 미만이면 모든 task trace가 존재하지 않는다.
- local 진단 stdout의 보존·접근 통제는 container host 운영 설정에 의존한다.
- 새로운 operational event field와 downstream dashboard 호환성을 자동 검증하는 외부 contract test는 없다.

## Update When

log format, operational marker/action/allowlist, 수집 sink, request/task/dependency cardinality, ErrorCode 규칙, Langfuse tree/content policy, redaction, failure isolation이 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/core/test_logging.py tests/core/test_operational_logging.py tests/core/test_redaction.py -q`
- `uv run pytest tests/core/test_langfuse_tracing.py tests/core/test_no_direct_elasticsearch.py tests/core/test_error_codes.py -q`
- `uv run pytest tests/api/test_request_logging.py tests/api/test_error_handlers.py tests/services/test_timeline_runner.py tests/services/test_user_memory_runner.py tests/services/test_app_server_client.py -q`
- `uv run pytest tests/scripts/test_filebeat_config.py -q`
- `rg -n "emit_event|report_error|event.dataset|capture_langfuse" app tests`

