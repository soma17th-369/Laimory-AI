# Timeline 실행 관측

Timeline 요청 하나의 입력 조회부터 Agent 처리, LLM 호출, RDB 저장, App Server 콜백까지를
구조화된 JSON 로그로 수집한다. 모든 로그의 상관키는 `taskId` 하나다. 별도의
`transactionId`, `traceId`, `spanId`는 만들지 않는다.

## 처리 흐름

`process_timeline_task`가 요청마다 전용 Observer와 메모리 버퍼를 만들고 `taskId`
컨텍스트를 연다. `contextvars`를 통해 `asyncio.to_thread`에서 실행되는 Agent와 LLM까지
같은 `taskId`가 전달된다. 콜백이 끝난 후 로그를 Elasticsearch `_bulk` API로 전송한다.

```text
REQUEST → MAIN_AGENT → EVENT_AGENT → TIMELINE_AGENT → REPAIR_AGENT
        → LLM(PROMPT/RESPONSE/FAILED) → STORAGE → CALLBACK → FINAL
```

관측 기능은 부가 기능이다. Elasticsearch 또는 로컬 저장이 실패해도 Timeline 상태,
RDB 저장, App Server 콜백에는 영향을 주지 않는다.

## 이벤트 계약

각 이벤트는 다음 필드를 사용한다.

- `schemaVersion`, `taskId`, `sequence`, `timestamp`
- `stage`, `eventType`, `agent`, `iteration`
- `provider`, `model`, `agentVersion`, `providerVersion`
- `durationMs`, 토큰 사용량
- 마스킹과 크기 제한이 적용된 `payload`

`sequence`는 Observer가 task 안에서 단조 증가하도록 발급한다. Elasticsearch 문서 `_id`는
전송 시 `taskId-sequence`로 파생하지만 로그 필드로 노출하지 않는다. App Server는 task마다
새 `taskId`를 발급하며 이미 사용한 값을 다른 실행에 재사용하지 않는다.

## payload 보안과 크기 제한

원문을 그대로 저장하는 모드는 없다.

- `SANITIZED`(기본): API key, Bearer token, 이메일, 전화번호, 민감 키를 마스킹한 본문 저장
- `NONE`: 본문 대신 byte 길이와 SHA-256만 저장
- `OBS_MAX_PAYLOAD_BYTES`를 넘으면 마스킹된 JSON의 앞부분만 `contentPreview`로 저장하고
  `truncated=true`, 원래 길이와 해시를 함께 남김
- `OBS_MAX_EVENTS_PER_TASK`를 넘으면 추가 이벤트를 버리고 task 요약의
  `droppedEventCount`에 기록. 단, FINAL/FAILED 이벤트는 우선 보존

`payload`는 Elasticsearch `_source`에는 보관하지만 색인하지 않는다. 따라서 Kibana에서
개별 문서를 열어 확인할 수 있으나 payload 내부 문자열은 검색·집계 대상이 아니다.
패턴 마스킹만으로 모든 개인정보를 제거할 수 있는 것은 아니므로 접근권한과 보존기간도
함께 제한해야 한다.

## Elasticsearch 문서

월별 인덱스 두 개를 사용한다.

- `agent-tasks-YYYY.MM`: task 요약 1건. `_id=taskId`
- `agent-events-YYYY.MM`: 수집된 이벤트 N건. `_id=taskId-sequence`

두 문서 모두 `taskId`를 저장한다. task 요약에는 전체 상태·처리시간·이벤트 수·LLM 호출
수·합산 토큰을, event 문서에는 단계·Agent·모델·지연시간·토큰·payload를 저장한다.

인덱스 템플릿은 Elasticsearch에 각각 그대로 적용할 수 있다.

```bash
curl -X PUT "$ES_URL/_index_template/agent-tasks" \
  -H 'Content-Type: application/json' \
  --data-binary @docs/observability/agent-tasks-index-template.json

curl -X PUT "$ES_URL/_index_template/agent-events" \
  -H 'Content-Type: application/json' \
  --data-binary @docs/observability/agent-events-index-template.json
```

## Kibana 조회

task의 전체 이벤트를 시간순으로 조회한다.

```json
GET agent-events-*/_search
{
  "query": { "term": { "taskId": "<taskId>" } },
  "sort": [
    { "sequence": "asc" }
  ]
}
```

task 요약은 다음처럼 조회한다.

```json
GET agent-tasks-*/_search
{
  "query": { "term": { "taskId": "<taskId>" } }
}
```

## 전송과 재시도

- `httpx`로 NDJSON `_bulk` 요청을 전송하며 마지막 newline을 포함한다.
- 약 5 MB 단위로 요청을 나눈다.
- 429와 5xx만 지수 백오프와 jitter를 적용해 재시도한다.
- Bulk 응답의 item별 부분 실패도 확인해 재시도 가능한 item만 다시 보낸다.
- mapping 오류 같은 영구 4xx와 재시도 소진은 로그만 남기고 Timeline 처리와 격리한다.

## 설정

| 키 | 기본값 | 설명 |
|---|---:|---|
| `OBS_ENABLED` | `false` | Elasticsearch 전송 스위치 |
| `OBS_LOCAL_DIR` | 없음 | 로컬 검증용 `task.json`, `events.jsonl` 저장 경로 |
| `OBS_CONTENT_CAPTURE` | `SANITIZED` | `SANITIZED` 또는 `NONE` |
| `OBS_MAX_PAYLOAD_BYTES` | `16384` | 이벤트 payload 최대 byte |
| `OBS_MAX_EVENTS_PER_TASK` | `1000` | task별 메모리 버퍼 이벤트 상한 |
| `ES_URL`, `ES_API_KEY` | 없음 | Elasticsearch 접속 정보 |
| `ES_TASK_INDEX` | `agent-tasks` | task 요약 인덱스 base |
| `ES_EVENT_INDEX` | `agent-events` | event 인덱스 base |
| `ES_TIMEOUT_SEC` | `5` | Bulk 요청 timeout |
| `ES_MAX_RETRIES` | `3` | 최초 요청 이후 최대 재시도 횟수 |
| `AGENT_VERSION` | 프로젝트 버전 | 배포·빌드 버전. 운영에서는 이미지 tag나 commit SHA 권장 |

`OBS_ENABLED=false`이고 `OBS_LOCAL_DIR`도 비어 있으면 Observer와 버퍼를 만들지 않으며,
payload 직렬화나 해시 계산도 하지 않는다.

## 운영 정책

- `taskId`는 일회성 식별자로 사용하고 재사용하지 않는다.
- `agent-tasks-*`, `agent-events-*`에 ILM 보존 정책을 적용한다.
- payload 열람 권한은 장애·품질 분석 담당자로 제한한다.
- 운영 환경에서는 `AGENT_VERSION`에 이미지 tag 또는 commit SHA를 넣는다.
