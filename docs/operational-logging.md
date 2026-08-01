# 운영 로그와 Filebeat 파이프라인

> 기준일: 2026-08-01 (이슈 #47, #53)
> 대상: EC2 컨테이너로 운영 중인 Laimory AI 서버

## 1. 관측 책임 경계

관측은 두 갈래고, 서로 대체하지 않는다.

| | Langfuse | Elasticsearch (이 문서) |
|---|---|---|
| 담당 | AI agent 실행 관측 | FastAPI 서버 운영 이벤트 |
| 내용 | agent 트리, LLM generation, 프롬프트·응답 본문, token usage, 단계별 진행 | 요청 완료, 서버 시작·종료, 백그라운드 작업 완료, 외부 연동 결과·재시도 |
| 경로 | 앱 → Langfuse SDK | 앱 stdout(JSON) → Filebeat 컨테이너 → Elasticsearch |
| 본문 | `LANGFUSE_CONTENT_CAPTURE` 정책에 따라 마스킹 후 저장 | **담지 않음** |

**애플리케이션은 Elasticsearch를 직접 호출하지 않는다.** ES URL도 자격증명도 앱
설정에 없다. 이 경계는 [`tests/core/test_no_direct_elasticsearch.py`](../tests/core/test_no_direct_elasticsearch.py)가
정적 검색으로 지킨다.

```text
laimory-ai 컨테이너 stdout (한 줄 JSON)
  → /var/lib/docker/containers/<id>/<id>-json.log
  → laimory-filebeat 컨테이너 (autodiscover, container name = laimory-ai)
  → event.dataset = laimory.api 인 줄만 통과 (그 밖은 drop_event)
  → Elasticsearch data stream  logs-laimory.ai-<env>
```

## 1-1. 수집되는 것은 운영 이벤트뿐이다 (이슈 #53)

컨테이너 stdout 에는 두 종류의 줄이 흐른다.

| | 운영 이벤트 | 일반 로그 |
|---|---|---|
| 만드는 곳 | [`app/core/operational_logging.py`](../app/core/operational_logging.py)의 `emit_event` | `get_logger()` 로 남기는 모든 줄 |
| 표식 | `event.dataset` = `laimory.api` | 없음 |
| 필드 | 이벤트별 **allowlist** 로 emitter 가 조립 | 호출부 자유(`log_fields`) |
| 수집 | Elasticsearch | **버려진다**(로컬·`docker logs` 진단용) |

경계를 표식 하나로 둔 이유는 **추가되는 쪽이 안전해야** 하기 때문이다. 로그 호출은
계속 늘어나는데 "이건 빼자" 를 매번 기억해야 하는 구조면 언젠가 사용자 콘텐츠가
섞인다. 아래 표에 없는 이벤트는 자동으로 수집 대상이 아니다.

방어선은 두 겹이다. 앱에서는 emitter 만 표식을 만들 수 있고(`event.*` 는 예약 필드라
일반 로그가 위조하지 못한다), Filebeat 에서는 표식이 정확히 일치하지 않으면 버린다.

### 이벤트 목록

| `event.action` | 언제 | 필드 |
|---|---|---|
| `http.request.completed` | HTTP 요청 하나가 끝날 때(요청당 1건) | `method`, `route`, `httpStatus`, `durationMs`, `errorCode`, `errorType`, `taskId` |
| `server.started` | lifespan 시작 | `appEnv`, `logFormat` |
| `server.stopped` | lifespan 종료 | `appEnv`, `uptimeMs` |
| `timeline.task.completed` | 202 로 접수한 백그라운드 작업이 끝날 때(작업당 1건) | `taskId`, `status`, `durationMs`, `callbackSent`, `errorCode`, `failureStage` |
| `dependency.request.completed` | App Server 논리 호출 하나가 끝날 때 | `dependency`, `operation`, `httpStatus`, `attempts`, `durationMs`, `errorCode`, `taskId`, `tokenRefreshCount` |
| `dependency.request.retry` | 그 호출의 재시도 한 번 | `dependency`, `operation`, `attempt`, `maxAttempts`, `reason`, `httpStatus`, `delayMs`, `taskId` |

공통 필드는 `timestamp`, `log.level`, `logger`(`app.operational`), `message`,
`service`, `environment`, `version`, 그리고 표식 3종(`event.dataset`,
`event.action`, `event.outcome`)이다. `event.outcome` 은 `success` | `failure` 다.

**`message` 로 집계하지 않는다.** 사람이 읽는 고정 문구일 뿐이고, 계약은
`event.action` 이다. 문구는 언제든 다듬을 수 있어야 한다.

### 레벨 기준

| 상황 | 레벨 |
|---|---|
| 2xx/3xx 응답, 서버 시작·종료, 외부 연동 성공, 작업 성공 | INFO |
| 4xx 응답, 외부 연동 재시도 | WARNING |
| 5xx 응답, 외부 연동 최종 실패, 작업 실패 | ERROR |

`GET /ping`·`GET /health` 는 **정상 응답이면 적재하지 않는다**. 배포 스크립트와
컨테이너 헬스체크가 수 초마다 두드리므로 남기면 운영 로그가 이걸로만 찬다.
4xx/5xx 로 답한 헬스체크는 남는다 — 그건 실제 장애 신호다.

## 2. 로그 한 줄의 계약

`LOG_FORMAT=json`이면 로그 한 줄은 **유효한 JSON 하나**다. 예외도 한 줄 안에 들어간다 —
여러 줄로 흘리면 Filebeat가 줄마다 다른 이벤트로 쪼갠다.

수집되는 운영 이벤트 한 줄:

```json
{"timestamp":"2026-08-01T04:12:07.882Z","log.level":"INFO",
 "logger":"app.operational","message":"Timeline 작업 완료",
 "service":"laimory-ai","environment":"prod","version":"sha-9f2c1b...-amd64-run-42-1",
 "event.dataset":"laimory.api","event.action":"timeline.task.completed",
 "event.outcome":"success","taskId":"1f0a...","status":"SUCCESS",
 "durationMs":184203.4,"callbackSent":true}
```

수집되지 않는 일반 로그 한 줄(표식이 없다):

```json
{"timestamp":"2026-08-01T04:12:07.882Z","log.level":"DEBUG",
 "logger":"app.services.timeline_runner","message":"단계 완료: STORAGE",
 "service":"laimory-ai","environment":"prod","version":"...",
 "taskId":"1f0a...","stage":"STORAGE","dailyRecordId":8814,"eventCount":7,
 "durationMs":812.417}
```

### 항상 있는 필드

| 필드 | 의미 |
|---|---|
| `timestamp` | UTC ISO8601(ms). Filebeat가 `@timestamp`로 옮긴다 |
| `log.level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `logger` | 모듈 경로(`app.services.timeline_runner` 등) |
| `message` | 사람이 읽는 한 줄. 기계가 읽을 값은 여기 넣지 않는다 |
| `service` | `laimory-ai` (Langfuse OTel `service.name`과 같은 값) |
| `environment` | `APP_ENV` (`prod`/`dev`/`local`) |
| `version` | `AGENT_VERSION`. EC2 배포는 이미지 태그가 들어간다 |

### 실행 컨텍스트가 자동으로 붙이는 필드 (일반 로그)

[`app/core/execution_context.py`](../app/core/execution_context.py)가 열려 있으면
호출부가 넘기지 않아도 붙는다. `contextvars`라 `asyncio.to_thread`로 넘어간 Event/
Timeline/Repair Agent 스레드까지 따라간다.

| 필드 | 의미 |
|---|---|
| `taskId` | 한 Timeline 처리의 상관키. **장애 추적의 시작점** |
| `stage` | `REQUEST`/`MAIN_AGENT`/`EVENT_AGENT`/`TIMELINE_AGENT`/`REPAIR_AGENT`/`LLM`/`STORAGE`/`CALLBACK`/`FINAL` |
| `agent` | Event/Repair Agent 이름 |
| `iteration` | Repair 반복 회차 |

운영 이벤트에는 이 값들이 자동으로 붙지 **않는다**. 이벤트가 허용한 필드만 나가고,
`taskId` 는 허용 이벤트에 한해 emitter 가 컨텍스트에서 채운다.

### 실패에 붙는 필드

`except` 블록은 [`report_error`](../app/core/exceptions.py)만 호출한다. 이 함수는
실패에 코드를 부여하고 **로컬 진단**으로 남긴다 — 표식이 없어 Elasticsearch 로
가지 않는다.

| 필드 | 의미 | 어디에 |
|---|---|---|
| `errorCode` | 정수 카탈로그 코드. API 응답·콜백과 **같은 값**([docs/error-codes.md](error-codes.md)) | 운영 이벤트 + 로컬 |
| `errorType` | 예외 클래스명 | HTTP 요청 이벤트 + 로컬 |
| `errorMessage` | 원본 예외 메시지(마스킹 후) | **로컬만** |
| `error.type` / `error.message` / `error.stack_trace` | `exc_info=True`인 최종 실패의 traceback | **로컬만** |

즉 Elasticsearch 에서 실패 원인은 `errorCode` + `failureStage` 로 좁히고, 원문
스택이 필요하면 `docker logs laimory-ai` 를 본다. Filebeat 에도 `error.message` /
`error.stack_trace` 를 지우는 방어선이 한 겹 더 있다.

### 단계 경계

`stage_span`이 각 단계의 시작·완료를 **DEBUG 로컬 진단**으로 남긴다. 단계별
소요시간과 중단 지점의 정본은 Langfuse 다.

```text
단계 시작: STORAGE
단계 완료: STORAGE   durationMs=812.417 eventCount=7
단계 중단: STORAGE   durationMs=95.2      ← 예외로 끊긴 경우
```

### 요청 이벤트

[`app/api/request_logging.py`](../app/api/request_logging.py) 미들웨어가 요청 하나를
`http.request.completed` **한 건**으로 닫는다. 실패한 요청도 한 건이다 — 오류 처리기는
자기 로그를 남기지 않고 안전한 `errorCode`/예외 클래스명만 요청 scope 에 적어 둔다.

- `route` 는 라우트 템플릿(`/v1/timeline`)이다. 경로 파라미터 값과 쿼리 문자열은
  남기지 않는다. 매칭되지 않은 요청만 길이를 자른 path 를 쓴다.
- `POST /v1/timeline`·`POST /invocations` 의 `durationMs` 는 **202 접수까지**다.
  이후 백그라운드 처리 시간은 `timeline.task.completed` 의 `durationMs` 가 답한다.
  두 이벤트는 `taskId` 로 이어진다.
- uvicorn의 access log는 [`align_uvicorn_loggers`](../app/core/logging.py)가 끈다 —
  JSON이 아니라 그대로 두면 이벤트가 버려지고, 남겨도 같은 요청이 두 줄이 된다.

## 3. 남기지 않는 것

Elasticsearch 로 나가는 이벤트에는 **위 표의 필드만** 실린다. 아래는 거기 없으므로
애초에 formatter 로 넘어가지 않는다.

- **프롬프트, LLM 응답, draft 전문, 사용자 원문, agent reasoning** — Langfuse 담당이다.
- **캘린더 제목·장소·주소·이벤트 제목·window 원문·파일명** — 사용자 콘텐츠다.
- **`rawId` 원문** — 수집 항목 식별자는 사용자 데이터다. 필요하면 건수만 남긴다.
- **URL·쿼리·요청/응답 body** — presigned URL 은 쿼리 자체가 자격증명이다(#52).
- **예외 원문과 traceback** — 코드(`errorCode`)와 클래스명까지다.
- **`taskToken`** — 값은 어떤 자리로도 나가지 않는다. 갱신 횟수(`tokenRefreshCount`)만 남는다.
- **API key, Authorization 헤더, AWS 자격증명** — 키 이름으로 걸러 `[REDACTED]`가 된다.
- **이메일·전화번호** — 메시지와 구조화 필드 값 모두 패턴으로 마스킹한다.

일반 로그(로컬 진단)에는 예외 원문·스택이 남을 수 있다. 그 줄은 표식이 없어
Elasticsearch 로 가지 않으며, `docker logs` 접근 권한과 로테이션 정책이 그대로
보호 정책이 된다.

allowlist 는 [`app/core/operational_logging.py`](../app/core/operational_logging.py)가,
마스킹은 [`app/core/redaction.py`](../app/core/redaction.py)가 소유한다. 계약은
[`tests/core/test_operational_logging.py`](../tests/core/test_operational_logging.py)와
[`tests/core/test_logging.py`](../tests/core/test_logging.py)가 검증하고, Filebeat
설정에도 같은 키를 지우는 `drop_fields`가 한 겹 더 있다
([`tests/scripts/test_filebeat_config.py`](../tests/scripts/test_filebeat_config.py)).

## 4. Elasticsearch에서 찾기

data stream은 `logs-laimory.ai-<env>`다. Kibana Discover에서 그 data stream을
data view로 잡고 아래 KQL을 쓴다.

### taskId 하나의 전체 흐름

```text
taskId : "1f0a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
```

시간순으로 `http.request.completed`(202 접수) → `dependency.request.completed`
(operation=input) → (AI 실행은 Langfuse) → `dependency.request.completed`
(operation=result, callback) → `timeline.task.completed` 가 이어진다.

Agent 단계·토큰·repair 상세는 여기 없다. 같은 `taskId` 로 Langfuse 에서 본다.

### 요청 상태와 응답시간

```text
event.action : "http.request.completed" and route : "/v1/timeline"
```

### 느린 백그라운드 작업 찾기

```text
event.action : "timeline.task.completed" and durationMs > 300000
```

### 오류 코드로 집계

```text
errorCode : 1201 and environment : "prod"
```

Kibana Lens에서 `errorCode`로 terms 집계를 걸면 어떤 실패가 늘고 있는지 바로 보인다.
어느 구간에서 깨졌는지는 `failureStage`(작업) 또는 `event.action`(요청/외부 연동)이
답한다.

### 실패한 것만

```text
event.outcome : "failure" and environment : "prod"
```

### 외부 연동 재시도가 늘고 있는지

```text
event.action : "dependency.request.retry" and operation : "result"
```

### `_search` API로

```bash
curl -s "$ES_HOSTS/logs-laimory.ai-prod/_search" \
  -H "Authorization: ApiKey $ES_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {"bool": {"filter": [
      {"term": {"taskId": "1f0a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"}}
    ]}},
    "sort": [{"@timestamp": "asc"}],
    "_source": ["@timestamp", "log.level", "event.action", "event.outcome",
                "errorCode", "durationMs"]
  }'
```

> `message` 로 검색하던 기존 대시보드는 `event.action` 기준으로 바꿔야 한다.
> 과거에 적재된 로그는 그대로 남는다(삭제하지 않는다).

## 5. smoke test

배포 전에 로컬에서 파이프라인을 확인하는 절차다. 실제 Elasticsearch 접속정보가 필요하다.

### 5.1 앱 로그가 유효한 JSON인지

```bash
LOG_FORMAT=json uv run uvicorn app.server:app --port 8000 2>&1 \
  | while IFS= read -r line; do
      printf '%s\n' "$line" | python -c 'import json,sys; json.loads(sys.stdin.read()); print("OK")' \
        || { echo "JSON 아님: $line"; }
    done
```

한 줄이라도 `JSON 아님`이 나오면 Filebeat가 그 이벤트를 잃는다.

### 5.2 필드 계약과 수집 경계

```bash
uv run pytest -m "not live_llm" \
  tests/core/test_logging.py tests/core/test_operational_logging.py \
  tests/core/test_no_direct_elasticsearch.py tests/scripts/test_filebeat_config.py \
  tests/api/test_request_logging.py tests/api/test_server_lifecycle.py
```

Filebeat 이미지를 쓸 수 있으면 설정 자체도 검사한다.

```bash
docker run --rm -v /tmp/filebeat.yml:/usr/share/filebeat/filebeat.yml:ro \
  docker.elastic.co/beats/filebeat:<태그> filebeat test config -e --strict.perms=false
```

### 5.3 컨테이너 두 개로 실제 적재까지

```bash
# 1) 앱 컨테이너
docker build -t laimory-ai:smoke .
docker run -d --name laimory-ai --env-file .env -e LOG_FORMAT=json \
  --log-opt max-size=20m --log-opt max-file=3 -p 8080:8080 laimory-ai:smoke

# 2) Filebeat 컨테이너 (설정은 템플릿을 복사해 채운다)
cp docs/observability/filebeat.example.yml /tmp/filebeat.yml
cat > /tmp/filebeat.env <<'ENV'
FILEBEAT_IMAGE=docker.elastic.co/beats/filebeat:<ES 버전에 맞춘 태그>
ES_HOSTS=https://<es-host>:9200
ES_API_KEY=<수집 전용 API key>
LAIMORY_ENV=dev
ENV
mkdir -p /tmp/filebeat-data
docker run -d --name laimory-filebeat --user root --env-file /tmp/filebeat.env \
  -v /tmp/filebeat.yml:/usr/share/filebeat/filebeat.yml:ro \
  -v /tmp/filebeat-data:/usr/share/filebeat/data \
  -v /var/lib/docker/containers:/var/lib/docker/containers:ro \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  "$(grep '^FILEBEAT_IMAGE=' /tmp/filebeat.env | cut -d= -f2-)" \
  filebeat -e --strict.perms=false

# 3) 요청을 흘리고
curl -s http://127.0.0.1:8080/ping

# 4) 적재 확인 (수 초 뒤)
curl -s "$ES_HOSTS/logs-laimory.ai-dev/_search?size=5&sort=@timestamp:desc" \
  -H "Authorization: ApiKey $ES_API_KEY"
```

확인할 것:

- `service` 가 `laimory-ai` 이고 `environment` 가 맞다
- `message` 가 원본 JSON 문자열이 아니라 사람이 읽는 한 줄이다(= `decode_json_fields` 성공)
- `@timestamp` 가 수집 시각이 아니라 앱이 찍은 시각이다(= `timestamp` processor 성공)
- `taskToken`, `apiKey` 같은 필드가 없다
- `laimory-filebeat` 자신의 로그는 들어오지 않는다

수집 경계(#53)까지 확인하려면 이어서 본다.

```bash
# 요청 몇 건을 흘린다(정상·404·검증 실패)
curl -s http://127.0.0.1:8080/ping
curl -s http://127.0.0.1:8080/v1/does-not-exist
curl -s -X POST http://127.0.0.1:8080/v1/timeline -H 'Content-Type: application/json' -d '{}'

# 적재된 것이 운영 이벤트뿐인지
curl -s "$ES_HOSTS/logs-laimory.ai-dev/_search?size=20&sort=@timestamp:desc" \
  -H "Authorization: ApiKey $ES_API_KEY" \
  | python -c 'import json,sys; [print(h["_source"].get("event",{}).get("action")) for h in json.load(sys.stdin)["hits"]["hits"]]'
```

- 모든 문서에 `event.dataset=laimory.api` 와 `event.action` 이 있다
- 표식이 없는 줄(Agent 진단, httpx, 비JSON stdout)은 **0건**이다
- 정상 `/ping` 은 없고, 404 와 422 는 `errorCode` 와 함께 있다
- `POST /v1/timeline` 접수 이벤트와 `timeline.task.completed` 가 같은 `taskId` 로 이어지고
  `durationMs` 가 서로 다르다(접수 응답시간 vs 백그라운드 전체 처리시간)

## 6. 문제가 생겼을 때

| 증상 | 확인 |
|---|---|
| ES에 아무것도 안 들어옴 | `docker logs laimory-filebeat` — 인증(401), 호스트 연결, `setup.template` 권한 오류. 그 다음 `decode_json_fields.expand_keys: true` 와 `drop_event` 조건의 필드 경로(`event.dataset`)를 본다. 표식을 읽지 못하면 **전부** 버려진다 |
| 특정 이벤트만 안 보임 | 앱에서 그 이벤트를 `emit_event` 로 남기는지, `_ALLOWED_FIELDS` 에 필드가 있는지. 허용 목록에 없는 필드는 조용히 빠진다(`app.core.operational_logging` 의 DEBUG 진단에 이름이 남는다) |
| 대시보드가 비었음 | `message` 기반 쿼리를 `event.action` 기준으로 바꿨는지(#53) |
| `message` 가 JSON 문자열 그대로 | 앱이 `LOG_FORMAT=json` 이 아니거나, `decode_json_fields` 대상 필드가 다르다 |
| `@timestamp` 가 전부 수집 시각 | `timestamp` processor의 `layouts` 가 앱 포맷과 어긋났다 |
| 같은 로그가 두 번 | registry 볼륨(`/opt/laimory-ai/filebeat-data`)이 마운트되지 않았다 |
| 배포 직후 몇 줄이 빔 | `close_removed: false` / `clean_removed: false` 가 설정에 있는지 |
| 디스크가 참 | 두 컨테이너 모두 `--log-opt max-size` 가 걸려 있는지(`docker inspect`) |

운영 절차와 롤백은 [docs/deploy-ec2.md](deploy-ec2.md) §11에 있다.
