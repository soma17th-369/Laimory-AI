# 운영 로그와 Filebeat 파이프라인

> 기준일: 2026-07-31 (이슈 #47)
> 대상: EC2 컨테이너로 운영 중인 Laimory AI 서버

## 1. 관측 책임 경계

관측은 두 갈래고, 서로 대체하지 않는다.

| | Langfuse | Elasticsearch (이 문서) |
|---|---|---|
| 담당 | AI agent 실행 관측 | FastAPI 서버·배포 환경 운영 로그 |
| 내용 | agent 트리, LLM generation, 프롬프트·응답 본문, token usage | 요청·처리 결과·오류·외부 API 연동·백그라운드 작업 |
| 경로 | 앱 → Langfuse SDK | 앱 stdout(JSON) → Filebeat 컨테이너 → Elasticsearch |
| 본문 | `LANGFUSE_CONTENT_CAPTURE` 정책에 따라 마스킹 후 저장 | **담지 않음** |

**애플리케이션은 Elasticsearch를 직접 호출하지 않는다.** ES URL도 자격증명도 앱
설정에 없다. 이 경계는 [`tests/core/test_no_direct_elasticsearch.py`](../tests/core/test_no_direct_elasticsearch.py)가
정적 검색으로 지킨다.

```text
laimory-ai 컨테이너 stdout (한 줄 JSON)
  → /var/lib/docker/containers/<id>/<id>-json.log
  → laimory-filebeat 컨테이너 (autodiscover, container name = laimory-ai)
  → Elasticsearch data stream  logs-laimory.ai-<env>
```

## 2. 로그 한 줄의 계약

`LOG_FORMAT=json`이면 로그 한 줄은 **유효한 JSON 하나**다. 예외도 한 줄 안에 들어간다 —
여러 줄로 흘리면 Filebeat가 줄마다 다른 이벤트로 쪼갠다.

```json
{"timestamp":"2026-07-31T04:12:07.882Z","log.level":"INFO",
 "logger":"app.services.timeline_runner","message":"단계 완료: STORAGE",
 "service":"laimory-ai","environment":"prod","version":"sha-9f2c1b...-amd64-run-42-1",
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

### 실행 컨텍스트가 자동으로 붙이는 필드

[`app/core/execution_context.py`](../app/core/execution_context.py)가 열려 있으면
호출부가 넘기지 않아도 붙는다. `contextvars`라 `asyncio.to_thread`로 넘어간 Event/
Timeline/Repair Agent 스레드까지 따라간다.

| 필드 | 의미 |
|---|---|
| `taskId` | 한 Timeline 처리의 상관키. **장애 추적의 시작점** |
| `stage` | `REQUEST`/`MAIN_AGENT`/`EVENT_AGENT`/`TIMELINE_AGENT`/`REPAIR_AGENT`/`LLM`/`STORAGE`/`CALLBACK`/`FINAL` |
| `agent` | Event/Repair Agent 이름 |
| `iteration` | Repair 반복 회차 |

### 실패에 붙는 필드

`except` 블록은 [`report_error`](../app/core/exceptions.py)만 호출한다.

| 필드 | 의미 |
|---|---|
| `errorCode` | 정수 카탈로그 코드. API 응답·콜백과 **같은 값**([docs/error-codes.md](error-codes.md)) |
| `errorType` | 예외 클래스명 |
| `errorMessage` | 원본 예외 메시지(마스킹 후). 로그에만 남고 외부로 나가지 않는다 |
| `error.type` / `error.message` / `error.stack_trace` | `exc_info=True`인 최종 실패에만 |

### 단계 경계

`stage_span`이 각 단계의 시작·완료를 남긴다. 본문은 없고 건수와 소요시간만 있다.

```text
단계 시작: STORAGE
단계 완료: STORAGE   durationMs=812.417 eventCount=7
단계 중단: STORAGE   durationMs=95.2      ← 예외로 끊긴 경우
```

### 요청 로그

[`app/api/request_logging.py`](../app/api/request_logging.py) 미들웨어가 요청 하나를
한 줄로 남긴다(`method`, `path`, `httpStatus`, `durationMs`). uvicorn의 access log는
[`align_uvicorn_loggers`](../app/core/logging.py)가 끈다 — JSON이 아니라 그대로 두면
이벤트가 통째로 버려지고, 남겨도 같은 요청이 두 줄이 된다.

`GET /ping`은 배포 스크립트가 수 초마다 두드리므로 `DEBUG`로 낮춘다. 쿼리 문자열은
남기지 않는다.

## 3. 남기지 않는 것

- **프롬프트, LLM 응답, draft 전문, 사용자 원문, agent reasoning** — Langfuse 담당이다.
- **`taskToken`** — 값은 어떤 자리로도 나가지 않는다. 갱신 횟수(`tokenRefreshCount`)만 남는다.
- **API key, Authorization 헤더, AWS 자격증명** — 키 이름으로 걸러 `[REDACTED]`가 된다.
- **이메일·전화번호** — 메시지와 구조화 필드 값 모두 패턴으로 마스킹한다.
- **`rawId` 원문** — 수집 항목 식별자는 사용자 데이터다. 위반 로그에는 건수만 남긴다.

마스킹은 [`app/core/redaction.py`](../app/core/redaction.py)가 하고,
[`tests/core/test_logging.py`](../tests/core/test_logging.py)가 검증한다.
Filebeat 설정에도 같은 키를 지우는 `drop_fields`가 한 겹 더 있다.

## 4. Elasticsearch에서 찾기

data stream은 `logs-laimory.ai-<env>`다. Kibana Discover에서 그 data stream을
data view로 잡고 아래 KQL을 쓴다.

### taskId 하나의 전체 흐름

```text
taskId : "1f0a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
```

시간순으로 보면 `단계 시작: REQUEST` → `단계 완료: REQUEST` → 각 Agent →
`단계 완료: STORAGE` → `완료 콜백 전송 완료` → `타임라인 처리 종료`가 이어진다.

### 오류 코드로 집계

```text
errorCode : 1201 and environment : "prod"
```

Kibana Lens에서 `errorCode`로 terms 집계를 걸면 어떤 실패가 늘고 있는지 바로 보인다.

### 레벨과 시간 범위

```text
log.level : ("ERROR" or "WARNING") and environment : "prod"
```

### 느린 단계 찾기

```text
message : "단계 완료*" and durationMs > 30000
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
    "_source": ["@timestamp", "log.level", "stage", "message", "errorCode", "durationMs"]
  }'
```

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

### 5.2 필수 필드와 taskId 상관

```bash
uv run pytest -m "not live_llm" tests/core/test_logging.py tests/core/test_no_direct_elasticsearch.py
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
  filebeat -e -strict.perms=false

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

## 6. 문제가 생겼을 때

| 증상 | 확인 |
|---|---|
| ES에 아무것도 안 들어옴 | `docker logs laimory-filebeat` — 인증(401), 호스트 연결, `setup.template` 권한 오류 |
| `message` 가 JSON 문자열 그대로 | 앱이 `LOG_FORMAT=json` 이 아니거나, `decode_json_fields` 대상 필드가 다르다 |
| `@timestamp` 가 전부 수집 시각 | `timestamp` processor의 `layouts` 가 앱 포맷과 어긋났다 |
| 같은 로그가 두 번 | registry 볼륨(`/opt/laimory-ai/filebeat-data`)이 마운트되지 않았다 |
| 배포 직후 몇 줄이 빔 | `close_removed: false` / `clean_removed: false` 가 설정에 있는지 |
| 디스크가 참 | 두 컨테이너 모두 `--log-opt max-size` 가 걸려 있는지(`docker inspect`) |

운영 절차와 롤백은 [docs/deploy-ec2.md](deploy-ec2.md) §11에 있다.
