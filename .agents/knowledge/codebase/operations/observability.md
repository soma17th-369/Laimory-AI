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

Elasticsearch 수집 대상은 `emit_event`만 붙일 수 있는 `event.dataset=laimory.api` 표식으로 제한한다. 현재 event action은 HTTP request 완료, container 시작·종료, Timeline task 완료, User Memory task 완료(`usermemory.task.completed`), App Server logical request 완료·retry, 기능 저하(`app.degraded`)다. 이벤트별 field allowlist가 있으며 allowlist 밖 값은 값 자체를 기록하지 않고 field name만 local DEBUG로 알린다.

`app.degraded`(#101)는 **task가 success로 끝나도 나가는 유일한 event다.** 흡수 경계가 exception을 삼키고 fallback으로 진행하므로 완료 event만으로는 Event Agent 하나가 통째로 죽은 것과 정상 처리를 구분할 수 없다. 같은 `taskId`로 두 줄을 묶어야 "성공했지만 무엇을 잃었는지"가 보인다. 저하 지점은 `component` 한 축이 답하고 값은 `ExecutionStage` 또는 `DegradedComponent`(`secret-bundle`·`langfuse`·`window`) 상수뿐이다. level은 WARNING, `event.outcome`은 `failure`다 — **`event.outcome: failure`만으로 실패 task를 세면 이제 성공 task의 저하가 섞이므로 `event.action` 조건을 함께 건다.**

event 필드는 값만이 아니라 **이름**도 수집 경로의 계약이다(#109). Filebeat 는 자기 수집기 정보를 `agent.*` 객체로 붙이고 `decode_json_fields`는 `target: ""`·`overwrite_keys: true`라 앱 값이 그것을 덮는다. 앱이 수집기가 객체로 채우는 이름(`agent`·`host`·`container`·`log`·`ecs`·`input`·`error`)을 최상위 scalar로 쓰면 같은 이름이 문서마다 객체와 문자열로 갈려 Elasticsearch가 그 문서를 거절한다 — stdout에는 찍히고 ES에는 없는 실패다. 그래서 Agent 이름은 `agentName`으로 나가고 `agent.*`는 수집기 몫으로 남긴다. 점이 든 이름(`event.dataset`·`log.level`·`error.type`)은 펴지면 양쪽 다 객체라 대상이 아니다. 이 규칙은 `tests/scripts/test_filebeat_config.py`가 앱의 고정 field와 event allowlist를 설정과 대조해 고정한다. 진단 줄의 `agent`는 표식이 없어 수집되지 않으므로 그대로 둔다.

발행은 `report_error`가 기본으로 한다. 항목 단위 loop(수집 항목마다·사진마다·도구 호출마다·응답 항목마다)는 한 task에서 수십 건이 되므로 `emit=False`로 빼고 잃은 양을 `droppedCount` 집계 1건으로 대신 낸다. LLM 실패(`llm.py`)는 호출 단위여도 발행한다 — `provider`·`model`·`stopReason`이 없으면 상위 흡수 경계에서 `EVENT_AGENT_FAILED`(1204)로 덮여 원인이 사라진다.

`server.started`/`server.stopped`의 `instanceId`는 process당 하나이고 emitter가 자동으로 채운다. AgentCore는 유휴 container를 회수하므로 한 log group에 여러 instance의 줄이 섞이고, 이 값이 cold start를 세고 시작·종료를 짝짓는 유일한 수단이다. **`server.stopped`는 강제 회수 시 lifespan `finally`가 돌지 않아 남지 않을 수 있다** — `uptimeMs`를 가동시간의 정본으로 쓰지 않는다.

HTTP request는 response start 시점에 요청당 한 건, Timeline·User Memory background task는 task당 한 건, App Server logical call은 retry 횟수와 무관하게 완료 한 건을 남긴다. 개별 retry는 별도 retry event다. 같은 `taskId`와 `errorCode`로 경계를 연결한다.

`message`는 사람이 읽는 한 줄이고 집계 계약은 여전히 `event.action`이다. 다만 dependency event만 문구를 `dependency`·`operation`·`event.outcome`의 고정 label 매핑으로 구체화한다(#78) — Kibana 목록에서 field를 펼치기 전에 timeline 입력 조회·결과 저장·완료 callback·User Memory 결과 저장을 구분해야 하기 때문이다. 문구에 들어가는 값은 emitter가 소유한 label 상수뿐이고, 등록되지 않은 `dependency`/`operation`은 문구에 싣지 않고 일반 문구로 통째로 폴백한다. 새 operation을 추가하면 label도 함께 등록한다. label이 없어도 field와 event 건수는 그대로다.

`usermemory.task.completed`에서 먼저 볼 field는 `resultSent`다. callback이 없는 계약이라 결과 저장 호출 한 번이 통보의 전부이고, `false`면 App Server는 아무 연락도 받지 못한 상태다. `droppedDailyTimelineCount`/`droppedEventCount`는 입력을 얼마나 잘랐는지를 남긴다 — 조용히 자르면 결과만 보고 "다 보고 이 정도"인지 "못 본 게 있어서 이 정도"인지 구분할 수 없다.

`ErrorCode`는 영역별 정수 대역, 외부 안전 메시지, 필요한 HTTP status의 단일 카탈로그다. 예약된 과거 번호는 재사용하지 않는다. `report_error`는 표식 없는 **진단 줄**과 표식 달린 `app.degraded`를 같은 code로 함께 낸다. 저하 event에는 allowlist를 통과한 field만 실리며 `context`의 임의 key는 들어가지 않는다(`tests/core/test_exceptions.py`가 고정한다). 외부 response·callback·운영 event는 같은 code를 참조한다.

**예외 원문과 traceback은 #53 경계의 의도된 예외다(#109 범위 확장).** 실패 event 두 개(`app.degraded`·`http.request.completed`)가 `errorMessage`·`errorStackTrace`를 싣는다. prod는 AgentCore가 container를 회수해 `docker logs`라는 선택지가 없어, 이것이 없으면 Kibana에서 실패를 보고도 원인을 볼 수단이 없다. 잔여 위험의 보호 경계는 field allowlist가 아니라 **index 접근 권한과 보존 정책**이다. 완화는 세 겹이다 — `redact_text` 마스킹, 길이 상한(message 1,000자 앞쪽·traceback 6,000자 **뒤쪽**, `…(잘림)` 표시), 검증 오류(422) 제외(그 문구는 사용자 입력 자체다). **마스킹이 자르기보다 먼저다** — `[REDACTED]`가 길이를 바꾸므로 순서가 반대면 상한이 실제로 나가는 줄을 재지 못하고, 상한의 목적은 docker json-file의 16KB 줄 분할(= event 통째 유실)을 피하는 것이다. `http.request.completed`의 traceback은 미처리 500에만 붙는다. **이 둘을 새 field의 선례로 삼지 않는다.**

이름이 camelCase(`errorMessage`)와 점 표기(`error.message`)로 갈린 것은 의도다. 앞은 emitter allowlist를 통과한 값이라 수집기가 남기고, 뒤는 표식 없는 일반 로그의 예외 field라 수집기가 지운다. 같은 이름이면 방어선 한 줄이 둘을 함께 지운다. 두 수집 경로(EC2 Filebeat `drop_fields`, prod 전달 Lambda의 denylist)가 **같은 목록을 써야** dev와 prod가 서로 다른 것을 적재하지 않는다. 정본은 `docs/observability/filebeat.example.yml`이고, Lambda는 저장소 밖이라 사람이 맞춘다.

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

- `get_logger()`로 남기는 일반 로그를 추가해도 자동으로 Elasticsearch 수집 범위가 넓어지지 않아야 한다. 단 `report_error` 호출은 예외다 — 저하 event를 함께 내므로 **건수**는 는다. 새 필드가 새는 것이 아니라(allowlist가 막는다) event 수가 느는 것이며, 항목 단위 loop에서는 `emit=False`로 막는다.
- 운영 이벤트에는 사용자 title, 장소, 주소, filename, prompt/response, URL, token을 넣지 않는다. exception 원문·traceback만 실패 event 2종에서 예외이며, 마스킹·길이 상한·422 제외를 함께 갖춘 경우에만 그렇다.
- 두 수집 경로(EC2 Filebeat, prod 전달 Lambda)의 field 제거 목록은 같아야 한다. 한쪽만 고치면 dev와 prod가 다른 것을 적재하고, 그 차이는 실패가 나기 전까지 보이지 않는다.
- event action 이름과 field 의미는 dashboard/search 계약이므로 임의 변경하지 않는다.
- 호출부가 넘긴 문자열은 어떤 경로로도 운영 event의 `message`가 되지 않는다. 문구는 emitter가 소유한 고정 label 조합뿐이고, 모르는 값은 문구에서 뺀다.
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
- prod 전달 Lambda(`laimory-agentcore-logs-to-es`)는 저장소 밖 콘솔 소스라 `drop_fields` 정본과의 일치를 test가 잡지 못한다. EC2 Filebeat 쪽만 `tests/scripts/test_filebeat_config.py`가 고정한다.

## Update When

log format, operational marker/action/allowlist, operational `message` 결정 규칙, 수집 sink, request/task/dependency cardinality, ErrorCode 규칙, Langfuse tree/content policy, redaction, failure isolation이 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/core/test_logging.py tests/core/test_operational_logging.py tests/core/test_redaction.py -q`
- `uv run pytest tests/core/test_langfuse_tracing.py tests/core/test_no_direct_elasticsearch.py tests/core/test_error_codes.py -q`
- `uv run pytest tests/api/test_request_logging.py tests/api/test_error_handlers.py tests/services/test_timeline_runner.py tests/services/test_user_memory_runner.py tests/services/test_app_server_client.py -q`
- `uv run pytest tests/scripts/test_filebeat_config.py -q`
- `rg -n "emit_event|report_error|event.dataset|capture_langfuse" app tests`

