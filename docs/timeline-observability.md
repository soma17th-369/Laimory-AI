# Timeline 실행 관측

Timeline 요청 하나가 입력 조회부터 Agent 처리, LLM 호출, RDB 저장, App Server 콜백까지 어떻게 실행됐는지 `taskId` 하나로 추적한다. 별도의 `transactionId`, `traceId`, `spanId`는 만들지 않는다.

관측 데이터는 목적에 따라 CloudWatch와 Elasticsearch로 나뉜다.

| 저장소 | 기록 내용 | 처리 방식 |
|---|---|---|
| CloudWatch | FastAPI·uvicorn 운영 로그, DB·콜백·ES 전송 오류, provider/model/token 사용량 요약 | 애플리케이션이 stdout에 JSON을 출력하고 CloudWatch가 수집 |
| Elasticsearch | 입력·프롬프트·응답·중간 결과와 단계·상태·소요 시간·토큰·모델 같은 Agent 실행 데이터 | 요청 중 메모리 버퍼에 모은 뒤 콜백 처리 후 `_bulk`로 일괄 전송 |

기본 `SANITIZED` 정책은 입력, 사용자 메모리, 프롬프트와 시스템 프롬프트, LLM 응답,
Event Agent 결과, timeline draft, repair plan, 도구 인자·결과를 payload에 남긴다.
저장 전 Secret과 이메일·전화번호 같은 식별 가능한 패턴을 마스킹하고 이벤트별 크기
제한을 적용한다. `NONE` 정책을 선택하면 본문 대신 byte 길이와 SHA-256만 기록한다.

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
- 마스킹된 실행 본문과 안전한 개수·불리언·오류 유형을 담는 `payload`

마지막 `FINAL` 이벤트에는 별도 요약 인덱스 대신 다음 집계를 붙인다.

- `taskDurationMs`: 첫 이벤트부터 FINAL까지의 전체 소요 시간
- `eventCount`: 저장 대상으로 만든 이벤트 수
- `droppedEventCount`: 버퍼 상한 때문에 버린 이벤트 수

주요 단계별 payload 예시는 다음과 같다.

| 단계 | 기록하는 값 |
|---|---|
| REQUEST | 정규화된 입력 전체, 입력 타입별 개수, 사용자 메모리 존재 여부 |
| MAIN/EVENT/TIMELINE | Agent 입력·결과·timeline과 후보·이벤트·질문·경고 개수 |
| LLM | 프롬프트·시스템 프롬프트·응답·옵션, 이미지 개수, provider/model, 소요 시간, 실제 응답 토큰 |
| REPAIR | 초기/수정 draft, repair plan, 도구 호출·결과와 반복·성공 여부 |
| STORAGE/CALLBACK | 시작·완료·실패 상태와 소요 시간 |
| VALIDATION_REPAIRED | rawId 원문 없이 `validationCode`, 대상 종류, 제거 참조 수, 제외 항목 수 |
| FAILED | 오류 메시지나 입력값이 아닌 `errorType`, `validationCode`, 위반 코드·건수 |

rawId allowlist 위반을 복구한 경우 `validationCode=SOURCE_RAW_ID_NOT_IN_REQUEST`와
`removedRefCount`, `droppedItemCount`를 남긴다. `SANITIZED` 정책에서는 함께 기록되는
입력·결과 본문에 rawId가 포함될 수 있다. 저장 계약을 끝내 충족하지 못한 경우에는
`validationCode=TIMELINE_STORAGE_CONTRACT_VIOLATION`, `violationCodes`,
`violationCount`를 남긴다. 검증 오류 원문은 외부 안전 메시지 정책에 따라 저장하지 않는다.

`payload`는 `_source`에는 보관하지만 Elasticsearch에서 색인하지 않는다. `SANITIZED`
정책은 API key, Bearer token, 이메일, 전화번호 및 민감 키 값을 마스킹한다. 마스킹된
payload가 `OBS_MAX_PAYLOAD_BYTES`를 넘으면 앞부분을 `contentPreview`로 저장하고 원래
길이와 SHA-256을 함께 남긴다. 패턴 마스킹만으로 위치·일정·건강 정보 같은 의미 기반
개인정보를 완전히 제거할 수 없으므로 인덱스 접근 권한과 보존 기간을 제한해야 한다.

## 전송과 실패 처리

- `httpx`로 newline이 포함된 NDJSON `_bulk` 요청을 보낸다.
- 요청은 최대 5 MB 단위로 나눈다.
- 429와 5xx만 지수 백오프와 jitter를 적용해 재시도한다.
- Bulk 응답의 item별 실패를 확인해 재시도 가능한 item만 다시 보낸다.
- mapping 오류 같은 영구 4xx와 최종 전송 실패는 CloudWatch 운영 로그에 남기고 Timeline 처리와 격리한다.
- `OBS_MAX_EVENTS_PER_TASK`를 넘으면 새 이벤트를 버리되 FINAL/FAILED 이벤트 공간을 우선 보존하고, 버린 수를 FINAL에 기록한다.

각 task의 전송 여부는 CloudWatch 운영 로그에서 다음 메시지로 확인한다. URL과 API key는
기록하지 않는다.

- `관측 수집 건너뜀`: Observer가 비활성화된 설정값을 함께 기록한다.
- `관측 task 초기화`: 처리 시작 시 수집·ES·로컬 출력 활성화 여부를 기록한다.
- `관측 ES 전송 건너뜀`: `OBS_ENABLED` 또는 `ES_URL` 조건이 충족되지 않은 상태를 기록한다.
- `관측 ES 전송 시작`: `taskId`, 문서 수, batch 수, index base를 기록한다.
- `관측 ES 전송 완료`: 최종 `attempted`, `succeeded`, `failed` 문서 수를 기록한다.
- 연결·HTTP·응답 파싱·item 오류와 재시도 소진은 원인별 warning으로 기록한다.

## Kibana 조회와 모니터링 항목

Kibana에서는 다음 Saved Object를 기준으로 조회한다.

- Data View: `AI Timeline Task` (`ai-timeline-task-*`, 시간 필드 `@timestamp`)
- Saved Discover: `AI Timeline Task 이벤트`
- 주요 컬럼: `taskId`, `sequence`, `stage`, `eventType`, `status`, `agentName`,
  `modelProvider`, `modelId`, `durationMs`, `taskDurationMs`, `tokenUsage.total`

로컬 `15601` 포트로 Kibana 터널을 열었다면 아래 주소에서 저장된 Discover 화면을
바로 열 수 있다.

```text
http://127.0.0.1:15601/kibana/app/discover#/view/ai-timeline-task-events
```

특정 실행은 KQL 검색창에 `taskId : "<taskId>"`를 입력하고 시간 범위를 해당 실행
시각이 포함되도록 설정해서 확인한다. 이벤트 실행 순서를 볼 때는 `sequence`를
오름차순으로 정렬한다.

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
- rawId 참조 복구 건수와 저장 계약 위반 코드
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
| `OBS_CONTENT_CAPTURE` | `SANITIZED` | `SANITIZED`(마스킹 본문) 또는 `NONE`(길이/해시) |
| `OBS_MAX_PAYLOAD_BYTES` | `262144` | 이벤트별 마스킹 payload 최대 byte |
| `OBS_MAX_EVENTS_PER_TASK` | `1000` | task별 메모리 버퍼 이벤트 상한 |
| `ES_URL`, `ES_API_KEY` | 없음 | Elasticsearch 접속 정보 |
| `ES_EVENT_INDEX` | `ai-timeline-task` | 이벤트 인덱스 base |
| `ES_TIMEOUT_SEC` | `5` | Bulk 요청 timeout |
| `ES_MAX_RETRIES` | `3` | 최초 요청 이후 최대 재시도 횟수 |
| `AGENT_VERSION` | 프로젝트 버전 | 배포·빌드 버전. 운영에서는 이미지 tag 또는 commit SHA 권장 |
| `LOG_FORMAT` | `rich` | 운영은 `json`으로 설정해 stdout을 CloudWatch에서 수집 |

`OBS_ENABLED=false`이고 `OBS_LOCAL_DIR`도 비어 있으면 Observer는 버퍼를 만들지 않는 no-op으로 동작한다.

## 실제 Elasticsearch smoke 테스트

private subnet의 Elasticsearch는 외부에 직접 공개하지 않는다. 로컬에서는 public
subnet의 WAS를 경유하는 SSH 또는 SSM 포트 포워딩을 열고, 애플리케이션에는 로컬
포워딩 주소를 설정한다.

SSM을 사용한다면 WAS 인스턴스를 대상으로 원격 호스트 포트 포워딩 세션을 연다.

```powershell
aws ssm start-session `
  --target <WAS_INSTANCE_ID> `
  --document-name AWS-StartPortForwardingSessionToRemoteHost `
  --parameters '{"host":["<ES_PRIVATE_HOST>"],"portNumber":["9200"],"localPortNumber":["19200"]}' `
  --profile <AWS_PROFILE> `
  --region ap-northeast-2
```

Kibana도 같은 방식으로 별도 터널을 열 수 있다.

```powershell
aws ssm start-session `
  --target <WAS_INSTANCE_ID> `
  --document-name AWS-StartPortForwardingSessionToRemoteHost `
  --parameters '{"host":["<KIBANA_PRIVATE_HOST>"],"portNumber":["5601"],"localPortNumber":["15601"]}' `
  --profile <AWS_PROFILE> `
  --region ap-northeast-2
```

SSH를 사용한다면 다음처럼 Elasticsearch 포트를 전달한다.

```powershell
ssh -i "<WAS_PEM_PATH>" -N `
  -L 19200:<ES_PRIVATE_HOST>:9200 `
  ec2-user@<WAS_PUBLIC_HOST>
```

터널을 연 터미널은 그대로 두고 `.env`를 설정한다.

```dotenv
OBS_ENABLED=true
ES_URL=http://127.0.0.1:19200
ES_API_KEY=
ES_EVENT_INDEX=ai-timeline-task
```

인증이 활성화된 서버는 `ES_API_KEY`를 채운다. 현재 exporter는 API key 인증을
지원하며, HTTPS를 사용하면 서버 인증서가 로컬에서도 신뢰되어야 한다.

다른 터미널에서 opt-in live 테스트를 실행한다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
$env:LAIMORY_LIVE_ES="1"
uv run pytest tests/integration/test_elasticsearch_live.py -q -s
```

테스트는 연결 확인, `ai-timeline-task` 템플릿 설치, 마스킹 본문 이벤트 전송,
refresh, `taskId` 재조회를 수행한다. 출력한 `taskId`의 smoke 문서는 Kibana 확인을
위해 삭제하지 않는다.

## 운영 정책

- `taskId`는 App Server가 발급한 상관키만 사용하고 재사용하지 않는다.
- `ai-timeline-task-*`에 접근 권한과 ILM 보존 정책을 적용한다.
- `SANITIZED`도 의미 기반 개인정보를 포함할 수 있으므로 운영 ES/Kibana 접근자를 제한한다.
- 운영 환경의 `AGENT_VERSION`에는 이미지 tag 또는 commit SHA를 넣는다.
- 본문 보존 기간과 삭제 절차를 운영 정책으로 명시한다.
