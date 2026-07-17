# 타임라인 테스트와 관측 로그

이 문서는 하루치 실제 입력으로 Event Agent부터 Repair Agent까지 실행하는 방법과,
하나의 Timeline 처리 과정에서 생성되는 관측 이벤트 계약을 설명합니다.

## 전체 처리와 관측 범위

```text
POST /v1/timeline
  → transactionId 생성
  → snapshot 조회·정규화
  → Main Agent
      → Event Agent 병렬 실행
      → Timeline Agent 초안 생성
      → Repair Agent 분석·도구 실행·재확정
  → 최종 TimelineDraft 저장·콜백
```

API가 생성한 `transactionId`는 정규화 요청과 비동기·스레드 실행 컨텍스트를 거쳐
모든 관측 이벤트에 동일하게 기록됩니다. `taskId`는 작업 상태를 조회하는 식별자이고,
`transactionId`는 한 번의 처리 흐름과 로그를 연결하는 상관관계 식별자입니다.

관측 단계(`stage`)는 `REQUEST`, `MAIN_AGENT`, `EVENT_AGENT`, `TIMELINE_AGENT`,
`REPAIR_AGENT`, `LLM`, `FINAL`입니다. 각 단계에서는 상황에 따라 다음 이벤트를
기록합니다.

| eventType | 의미 |
| --- | --- |
| `STARTED` | 단계 또는 Agent 실행 시작 |
| `COMPLETED` | 정상 완료와 결과 |
| `FAILED` | 호출·파싱·처리 실패 |
| `PROMPT` / `RESPONSE` | LLM 요청과 응답 |
| `PLAN` | Repair Agent가 만든 문제 분석·도구 계획 |
| `TOOL_CALL` | Repair 도구 호출과 결과 |
| `DRAFT_UPDATED` | 코드 확정 또는 도구 실행 후 갱신된 초안 |

## 관측 이벤트 계약

JSONL 파일은 한 줄에 JSON 객체 하나를 기록합니다. 선택 필드는 값이 있을 때만
포함됩니다.

```json
{
  "schemaVersion": "1",
  "transactionId": "tx-123",
  "timestamp": "2026-07-17T12:00:00Z",
  "stage": "LLM",
  "eventType": "RESPONSE",
  "agent": "timeline",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "durationMs": 842.3,
  "inputTokens": 1250,
  "outputTokens": 310,
  "totalTokens": 1560,
  "cachedTokens": 800,
  "reasoningTokens": 0,
  "payload": {
    "response": "..."
  }
}
```

공통 필드의 의미는 다음과 같습니다.

| 필드 | 의미 |
| --- | --- |
| `transactionId` | 요청부터 최종 결과까지 공유하는 식별자 |
| `agent` | 현재 Event/Timeline/Repair Agent 이름 |
| `iteration` | Repair 반복 횟수(1부터 시작) |
| `durationMs` | provider 또는 단계 실행 시간 |
| `inputTokens` | provider가 보고한 입력 토큰 |
| `outputTokens` | provider가 보고한 생성 결과 토큰 |
| `totalTokens` | provider가 보고한 전체 토큰 |
| `cachedTokens` | 캐시된 입력 토큰 |
| `reasoningTokens` | OpenAI reasoning 또는 Gemini thoughts 토큰 |
| `toolTokens` | Gemini가 별도로 보고한 도구 결과 재입력 토큰 |

토큰 수는 문자열 길이로 추정하지 않고 OpenAI `usage` 또는 Gemini
`usage_metadata`의 서버 보고값을 사용합니다. `totalTokens`는 provider 값이
기준이며 다른 필드의 단순 합으로 다시 계산하지 않습니다. provider가 제공하지 않은
값은 `0`으로 기록하지 않고 필드를 생략합니다.

## 콘텐츠 보호 정책

`Observer`는 sink에 전달하기 전에 `ContentCapture` 정책을 적용합니다.

- `NONE`: 기본 정책입니다. payload 본문 대신 직렬화 크기와 SHA-256만 남깁니다.
- `SANITIZED`: live 디버깅용입니다. 본문을 남기되 API key, Bearer token, 비밀번호,
  이메일, 전화번호 등 알려진 민감값을 `[REDACTED]`로 바꿉니다.
- 이미지 바이너리는 어떤 정책에서도 기록하지 않고 MIME type과 byte length만 남깁니다.

마스킹은 안전한 운영 로그 저장을 보장하는 완전한 DLP가 아닙니다. 실제 사용자
데이터가 포함된 `SANITIZED` 로그도 제한된 디버깅 자료로 취급해야 합니다.

## Sink와 실패 격리

현재 제공하는 sink는 다음과 같습니다.

- `NullObservationSink`: 기록하지 않는 서버 기본값
- `InMemoryObservationSink`: 테스트 검증용
- `JsonLinesObservationSink`: live 실행의 `observations.jsonl` 저장용
- `CompositeObservationSink`: 여러 sink에 동시에 전달

관측 sink의 예외는 Timeline 처리로 전파되지 않습니다. 복합 sink 중 하나가
실패해도 나머지 sink 기록을 계속 시도하며, LLM 응답과 최종 Timeline 결과는 관측
장애 때문에 실패하지 않습니다. 운영 환경에서 외부 로그 저장소를 사용하려면 해당
sink로 만든 `Observer`를 파이프라인 진입점에 주입해야 합니다.

## 날짜별 실제 입력

실제 LLM fixture는 다음 구조를 사용합니다.

```text
data/input/<YYYY-MM-DD>/
├── <YYYY-MM-DD>.json
├── 000_*.jpg
└── ...
```

JSON과 그 JSON이 참조하는 사진은 같은 날짜 디렉터리에 둡니다. 기본 날짜는
`2026-07-08`이며 `LAIMORY_LIVE_DATA_DATE`로 바꿀 수 있습니다.

## 실행별 출력

실제 LLM 실행은 기존 결과를 덮어쓰지 않고 다음 경로에 누적됩니다.

```text
data/output/runs/<data-date>/<run-id>-<provider>-<model>/
├── metadata.json
├── observations.jsonl
├── event-agents/
│   └── <agent>.json
├── timeline-draft.actual.json
└── timeline-draft.diff.txt
```

같은 pytest 프로세스의 동일 날짜·provider·model 테스트는 하나의 run을 공유합니다.
서로 다른 프로세스 결과를 묶어야 할 때만 같은 `LAIMORY_LIVE_RUN_ID`를 지정합니다.
`data/output/runs/`는 Git에서 제외됩니다.

## 테스트 명령

Windows에서 uv 기본 캐시 권한 문제가 있으면 먼저 로컬 캐시를 지정합니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
```

네트워크와 실제 LLM 비용이 없는 전체 테스트:

```powershell
uv run pytest tests -m "not live_llm"
```

관측 계약·마스킹·토큰 매핑·sink 실패 격리만 검증:

```powershell
uv run pytest tests/core -q
```

실제 LLM으로 특정 Event Agent 실행:

```powershell
$env:LAIMORY_LIVE_LLM="1"
$env:LAIMORY_LIVE_DATA_DATE="2026-07-08"
uv run pytest tests/agents/test_location_event_agent_live_input.py -s
```

실제 LLM으로 Event → Timeline → Repair 전체 실행:

```powershell
$env:LAIMORY_LIVE_LLM="1"
uv run pytest tests/integration/test_live_llm_data_fixture.py -s
```

결과가 예상 JSON과 완전히 같아야 성공하도록 강제하려면
`LAIMORY_LIVE_LLM_STRICT=1`을 추가합니다. 기본 live 통합 테스트는 확률적인 LLM
결과를 고려해 최종 계약의 날짜·timezone·배열 구조를 검사하고 실제 결과와 diff를
파일로 남깁니다. 다만 실제 provider 연결을 검증하는 테스트이므로 LLM `RESPONSE`가
하나도 없거나 `FAILED` 이벤트가 하나라도 있으면 fallback 결과가 생성되어도 실패합니다.

## 자동 테스트가 보장하는 항목

- 날짜 형식과 snapshot 경로 검증
- run 디렉터리 분리, 메타데이터와 안전한 상대 경로 저장
- API에서 생성한 `transactionId`의 요청·스레드·Agent 간 전파
- Main/Event/Timeline/Repair/LLM 단계의 관측 이벤트 연결
- Repair plan, 도구 호출, 확정 초안의 반복별 기록
- Secret·이메일·전화번호 마스킹과 이미지 바이너리 미기록
- OpenAI/Gemini 서버 usage의 공통 토큰 필드 변환
- usage 누락 시 미기록, LLM 실패 재전파, sink 실패 격리
- 최종 `TimelineDraft` 계약과 예상 fixture 비교
