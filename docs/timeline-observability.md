# Timeline 실행 관측

Timeline 요청 하나가 입력 조회부터 Agent 처리, LLM 호출, RDB 저장, App Server 콜백까지 어떻게 실행됐는지 `taskId` 하나로 추적한다. 별도의 `transactionId`, `traceId`, `spanId`는 만들지 않는다.

관측 데이터는 목적에 따라 CloudWatch와 Elasticsearch로 나뉜다.

| 저장소 | 기록 내용 | 처리 방식 |
|---|---|---|
| CloudWatch | FastAPI·uvicorn 운영 로그, DB·콜백·ES 전송 오류, provider/model/token 사용량 요약 | 애플리케이션이 stdout에 JSON을 출력하고 CloudWatch가 수집 |
| Elasticsearch | 단계·상태·소요 시간·토큰·모델·안전한 개수/유형 같은 Agent 실행 메타데이터 | 요청 중 메모리 버퍼에 모은 뒤 콜백 처리 후 `_bulk`로 일괄 전송 |

입력 원문, 사용자 메모리, 프롬프트와 시스템 프롬프트, LLM 응답 본문, timeline draft, repair plan, 도구 인자와 결과 본문은 어느 저장소에도 관측 데이터로 남기지 않는다. 필요한 경우 본문 대신 byte 길이와 SHA-256만 기록해 같은 내용인지 비교할 수 있게 한다.

## 처리 흐름

`process_timeline_task`가 요청별 Observer와 제한된 메모리 버퍼를 만들고 `taskId` 컨텍스트를 설정한다. `contextvars`를 사용하므로 `asyncio.to_thread`에서 실행되는 Agent와 LLM에도 같은 `taskId`가 전달된다.

```text
REQUEST → MAIN_AGENT → EVENT_AGENT → TIMELINE_AGENT → REPAIR_AGENT
        ↘ LLM(PROMPT/RESPONSE/FAILED)
        → STORAGE → CALLBACK → FINAL → Elasticsearch _bulk
```

관측은 부가 기능이다. 로컬 파일 기록이나 Elasticsearch 전송이 실패해도 Timeline 상태, RDB 저장, App Server 콜백 결과에는 영향을 주지 않는다.

## Elasticsearch 이벤트

`ai-timeline-task-YYYY.MM` 인덱스 하나만 사용한다. 이벤트 `_id`는 재전송해도 중복되지 않도록 `taskId-sequence`로 만든다.

공통 필드는 다음과 같다.

- `schemaVersion`, `taskId`, `sequence`, `timestamp`
- `stage`, `eventType`, `status`, `agent`, `iteration`
- `provider`, `model`, `agentVersion`, `providerVersion`
- `durationMs`, `inputTokens`, `outputTokens`, `totalTokens`, `cachedTokens`, `reasoningTokens`
- 안전한 개수·불리언·오류 유형과 본문 길이/해시를 담는 `payload`

마지막 `FINAL` 이벤트에는 별도 요약 인덱스 대신 다음 집계를 붙인다.

- `taskDurationMs`: 첫 이벤트부터 FINAL까지의 전체 소요 시간
- `eventCount`: 저장 대상으로 만든 이벤트 수
- `droppedEventCount`: 버퍼 상한 때문에 버린 이벤트 수

주요 단계별 payload 예시는 다음과 같다.

| 단계 | 기록하는 값 |
|---|---|
| REQUEST | 입력 타입별 개수, 사용자 메모리 존재 여부, 입력 전체의 길이/해시 |
| MAIN/EVENT/TIMELINE | Agent 수, 후보·이벤트·질문·경고 개수 |
| LLM | 프롬프트·응답의 길이/해시, 이미지 개수, provider/model, 소요 시간, 실제 응답 토큰 |
| REPAIR | 반복 횟수, 문제·도구 호출 개수, 도구 이름, 인자 이름, 성공 여부 |
| STORAGE/CALLBACK | 시작·완료·실패 상태와 소요 시간 |
| FAILED | 오류 메시지나 입력값이 아닌 `errorType`과 안전한 실패 분류 |

`payload`는 `_source`에는 보관하지만 Elasticsearch에서 색인하지 않는다. 본문 키는 중앙 정책에서 길이/해시로 치환하고, 남은 메타데이터의 API key, Bearer token, 이메일, 전화번호 같은 값은 마스킹한다. 메타데이터 자체가 `OBS_MAX_PAYLOAD_BYTES`를 넘으면 전체를 길이/해시로 축약한다.

## 전송과 실패 처리

- `httpx`로 newline이 포함된 NDJSON `_bulk` 요청을 보낸다.
- 요청은 최대 5 MB 단위로 나눈다.
- 429와 5xx만 지수 백오프와 jitter를 적용해 재시도한다.
- Bulk 응답의 item별 실패를 확인해 재시도 가능한 item만 다시 보낸다.
- mapping 오류 같은 영구 4xx와 최종 전송 실패는 CloudWatch 운영 로그에 남기고 Timeline 처리와 격리한다.
- `OBS_MAX_EVENTS_PER_TASK`를 넘으면 새 이벤트를 버리되 FINAL/FAILED 이벤트 공간을 우선 보존하고, 버린 수를 FINAL에 기록한다.

## Kibana 조회와 모니터링 항목

task 전체 흐름은 다음처럼 조회한다.

```json
GET ai-timeline-task-*/_search
{
  "query": { "term": { "taskId": "<taskId>" } },
  "sort": [
    { "sequence": "asc" }
  ]
}
```

이 이벤트로 다음을 모니터링한다.

- task 성공·실패·timeout과 단계별 진행 흐름
- Agent/단계별 오류율과 병목 구간
- provider/model별 LLM 지연 시간과 토큰 사용량
- repair 반복·도구 실패·fallback 발생
- 버퍼 상한으로 누락된 이벤트 발생 여부

## 인덱스 템플릿

```bash
curl -X PUT "$ES_URL/_index_template/ai-timeline-task" \
  -H 'Content-Type: application/json' \
  --data-binary @docs/observability/ai-timeline-task-index-template.json
```

## 설정

| 키 | 기본값 | 설명 |
|---|---:|---|
| `OBS_ENABLED` | `false` | Elasticsearch 전송 스위치 |
| `OBS_LOCAL_DIR` | 없음 | 로컬 검증용 `events.jsonl` 저장 경로 |
| `OBS_MAX_PAYLOAD_BYTES` | `16384` | 이벤트 payload 메타데이터 최대 byte |
| `OBS_MAX_EVENTS_PER_TASK` | `1000` | task별 메모리 버퍼 이벤트 상한 |
| `ES_URL`, `ES_API_KEY` | 없음 | Elasticsearch 접속 정보 |
| `ES_EVENT_INDEX` | `ai-timeline-task` | 이벤트 인덱스 base |
| `ES_TIMEOUT_SEC` | `5` | Bulk 요청 timeout |
| `ES_MAX_RETRIES` | `3` | 최초 요청 이후 최대 재시도 횟수 |
| `AGENT_VERSION` | 프로젝트 버전 | 배포·빌드 버전. 운영에서는 이미지 tag 또는 commit SHA 권장 |
| `LOG_FORMAT` | `rich` | 운영은 `json`으로 설정해 stdout을 CloudWatch에서 수집 |

`OBS_ENABLED=false`이고 `OBS_LOCAL_DIR`도 비어 있으면 Observer는 버퍼를 만들지 않는 no-op으로 동작한다.

## 운영 정책

- `taskId`는 App Server가 발급한 상관키만 사용하고 재사용하지 않는다.
- `ai-timeline-task-*`에 접근 권한과 ILM 보존 정책을 적용한다.
- 운영 환경의 `AGENT_VERSION`에는 이미지 tag 또는 commit SHA를 넣는다.
- 프롬프트나 응답 본문이 필요해지는 경우에도 이 관측 파이프라인에 임의로 추가하지 않고 별도 보안·보존 정책을 먼저 결정한다.
