# Langfuse Timeline 추적

Timeline 요청 한 건을 Langfuse trace 한 건으로 기록한다. 이 trace는 입력 조회부터
정규화, 다중 Agent, LLM, Repair, 저장, 콜백까지 Main Agent의 전체 흐름을 보여 준다.
Elasticsearch 관측은 오류 코드와 단계별 운영 이벤트의 정본으로 계속 사용하며,
Langfuse는 Agent 계층, 입출력, 모델 지연 시간, 토큰과 비용을 분석하는 선택적
관측 계층이다.

프로젝트는 Langfuse Python SDK `4.14.1`을 사용한다.

## 실제 Timeline trace 구조

trace와 observation 이름은 dashboard, evaluator, saved view가 참조하는 안정적인
계약이다. 실행별 값은 이름에 넣지 않고 metadata에 기록한다.

```text
generate-timeline (span, trace 실행 경계)
├─ retrieve-source-snapshot (span)
├─ normalize-source-snapshot (span)
├─ main-agent (agent)
│  ├─ event-agent-<source> (agent, 병렬 N개)
│  │  └─ infer-<source>-events 또는 describe-photo-images (generation)
│  ├─ merge-event-results (chain, 병렬 fan-in)
│  ├─ timeline-agent (agent)
│  │  └─ generate-timeline-draft (generation)
│  └─ repair-agent (agent)
│     ├─ confirm-timeline-draft (span)
│     ├─ analyze-repair-iteration (chain, 반복)
│     │  └─ analyze-timeline-repair (generation)
│     ├─ execute-repair-plan (chain, 반복)
│     │  └─ execute-<tool> (span)
│     └─ confirm-repair-iteration (chain, 반복)
├─ store-timeline (span)
├─ send-completion-callback (span)
└─ finalize-timeline (span)
```

Event Agent는 실제 요청에 해당 source가 있을 때만 만들어진다. Repair 분석과 실행도
수정이 필요할 때만 반복되므로 trace마다 observation 수는 달라질 수 있다.

Langfuse Agent Graph에는 실제 Agent 외에 두 종류의 구조 노드만 나타난다.
`merge-event-results`는 병렬 Event Agent가 Timeline Agent로 합류하는 fan-in이고,
Repair의 `analyze → execute → confirm` chain은 반복될 때 `Aggregated` 화면에서
cycle로 표현된다. 조회·정규화·개별 도구·저장·콜백은 graph를 복잡하게 만들지 않도록
`span`으로 기록하되 trace tree에서는 모두 펼쳐 보고 클릭할 수 있다. `Expanded`는
반복을 실행 순서대로 펼쳐 보여 준다. 연결 확인용 smoke trace는 이 구조를 검증하지
않는다.

## 화면에서 확인할 데이터

### 입력과 출력

콘텐츠 정책이 `SANITIZED`이면(local/dev 기본값) 다음 데이터가 각 단계의 input/output에
마스킹된 형태로 들어간다.

- root: 정규화 전후 요청, 최종 Timeline, 성공·실패 상태
- 조회·정규화: source snapshot과 정규화된 요청
- Event Agent: Agent별 전체 요청과 후보·fragment·warning 결과
- Timeline Agent: 병합 후보, 생성한 Timeline draft
- Repair: 초기·수정 draft, 반복별 plan, 도구 인자·결과, 확정 Timeline
- 저장·콜백: 저장할 Timeline과 콜백 payload, 처리 결과
- generation: `system`/`user` 역할의 전체 prompt와 `assistant` 응답

generation은 표준 role message 배열로 저장하므로 Langfuse가 대화형으로 렌더링한다.
provider, model, temperature, 이미지 개수와 MIME type도 함께 기록한다. 이미지 원본
bytes는 기록하지 않는다.

payload가 `LANGFUSE_MAX_PAYLOAD_BYTES`를 넘으면 본문이 preview, 원래 byte 길이,
SHA-256을 포함한 잘림 요약으로 바뀐다. 이때도 진단 지표는 그대로 남는다 — 크기 때문에
`durationMs`나 `errorCode`까지 잃지 않는다. callback token, API key, Authorization,
AWS 자격증명과 사진 원본은 어떤 정책에서도 외부로 보내지 않는다.

### taskId 검색

`taskId`는 모든 observation의 `metadata.taskId`와 Langfuse 전용 `sessionId`에 함께
전파한다. Traces 화면의 Fast 검색창에서는 다음 중 하나로 찾는다.

```text
session:=<taskId>
metadata.taskId:=<taskId>
```

`session`은 전용 인덱스 필드라 일상적인 task 조회에 사용하고,
`metadata.taskId`는 다른 metadata 조건과 조합할 때 사용한다. Langfuse 내부 trace
ID도 `taskId`로부터 결정적으로 생성하므로 같은 task의 재시도와 외부 관측을 교차
조회할 수 있다.

### 소요 시간

Langfuse UI의 observation start/end 시간으로 각 단계의 latency가 자동 계산된다.
또한 애플리케이션에서 측정한 밀리초를 각 observation의
`output.durationMs`에 명시적으로 기록한다. 최상위 `generate-timeline`의
`durationMs`는 조회부터 저장·콜백까지 task 전체 시간이다. `durationMs`는 진단 지표라
`NONE` 정책에서도 남는다.

### 토큰과 비용

실제 LLM 호출은 모두 `generation` observation이다. 각 generation에 다음 값이
기록된다.

- provider와 실제 model
- provider 응답이 보고한 input/output token
- provider가 제공하면 cache, reasoning, tool-use token 세부 bucket
- Langfuse 모델 가격표와 model 이름이 일치할 때 계산되는 비용

토큰은 tokenizer로 추정하지 않는다. provider 응답에 usage가 없으면 0을 지어내지
않고 해당 bucket을 비워 둔다.

Main Agent, Event Agent, Timeline Agent, Repair Agent와 최상위 task에는 하위
generation의 합계를 `output.tokenUsage`로 함께 기록한다. `tokenUsage`도 진단 지표라
`NONE` 정책에서 하위 집계까지 그대로 남는다.

```json
{
  "generationCount": 3,
  "inputTokens": 1200,
  "outputTokens": 340,
  "totalTokens": 1540,
  "byType": {
    "input": 1100,
    "input_cached_tokens": 100,
    "output": 300,
    "output_reasoning_tokens": 40
  }
}
```

Langfuse의 토큰·비용 차트는 `generation.usageDetails`를 기준으로 보고,
Agent/root의 `tokenUsage`는 해당 범위의 합계를 한눈에 확인할 때 사용한다.

비용은 Langfuse가 generation의 `model`과 프로젝트 모델 가격 정의를 매칭해 계산한다.
현재 기본 Bedrock 모델 `global.amazon.nova-2-lite-v1:0`은 Langfuse 내장 가격 정의에
없으므로 Project Settings → Models에 해당 모델의 정규식과 AWS 단가를 한 번 등록해야
한다. OpenAI와 Gemini도 동일한 방식이지만, 내장 모델명과 일치하면 별도 등록 없이
자동 계산된다. 모델 정의 변경은 새로 수집되는 generation부터 적용되며 기존 trace에는
소급되지 않는다.

### 오류

실패 observation은 `ERROR` level과 안전한 `statusMessage`를 사용한다.
애플리케이션 오류는 `output.errorCode`에 `app/core/error_codes.py`의 정수 코드를
기록한다. `errorCode`와 `errorType`은 진단 지표라 `NONE` 정책에서도 남으므로, 본문을
내보내지 않는 환경에서도 어느 단계가 어떤 코드로 실패했는지는 화면에서 확인할 수 있다.
원본 예외 메시지나 Secret은 Langfuse로 보내지 않는다.

## 콘텐츠 정책

Timeline에는 위치, 건강, 캘린더, 알림, 사진 메타데이터가 포함될 수 있다.
`LANGFUSE_CONTENT_CAPTURE`를 지정하지 않으면 실행 환경으로 정한다. `APP_ENV`가
`local` 또는 `dev`면 `SANITIZED`, 그 밖에는 `NONE`이다. 값을 지정하면 그 값이 항상 이긴다.

| 정책 | Langfuse input/output |
|---|---|
| `NONE` | 진단 지표만 남기고 나머지는 `body` 하나로 접어 길이와 SHA-256만 기록 |
| `SANITIZED` | Secret과 식별 가능한 개인정보 패턴을 마스킹한 본문 |

`NONE`에서도 사라지지 않는 진단 지표는 다음과 같다. 본문이 아니라 숫자·열거형·식별자라
외부로 나가도 안전하고, 이게 없으면 어느 단계가 얼마나 걸렸는지조차 알 수 없다.

`agent`, `agentCount`, `dailyRecordId`, `durationMs`, `errorCode`, `errorType`,
`generationCount`, `iteration`, `maxIterations`, `model`, `ok`, `provider`, `sequence`,
`stage`, `status`, `taskId`, `tokenUsage`, `tool`, `usageDetails`

`NONE`에서 도구 span의 output은 이렇게 남는다.

```json
{
  "ok": true,
  "durationMs": 812.4,
  "tokenUsage": { "generationCount": 1, "inputTokens": 1200, "outputTokens": 340 },
  "body": { "contentCaptured": false, "byteLength": 13849, "sha256": "da4991..." }
}
```

목록에 없는 키는 전부 본문으로 보고 접는다. 새 키가 생겨도 본문이 새지 않게 하려는
것이므로(기본 차단), 진단 지표를 추가하려면 `app/core/observability/redaction.py`의
`_DIAGNOSTIC_KEYS`에 명시적으로 넣어야 한다.

metadata에는 이 기본 차단을 적용하지 않는다. metadata는 호출부가 직접 고른 라벨이라
본문이 아니며, 본문 키만 골라 지우는 기존 규칙을 그대로 쓴다.

`SANITIZED`는 완전한 익명화가 아니다. 위치·일정·건강 정보처럼 패턴만으로 제거하기
어려운 내용은 남을 수 있다. 운영에서 활성화하기 전에 처리 근거, 프로젝트 접근 권한,
보존 기간을 검토해야 한다. SDK export 직전에도 OTel 문자열 attribute를 다시
마스킹한다.

## Elasticsearch와의 역할 분리

두 저장소는 목적이 다르므로 둘 다 유지한다. 같은 실행을 양쪽에서 찾을 때는 `taskId`를
쓴다(Elasticsearch는 `taskId` 필드, Langfuse는 `session`).

| 항목 | Elasticsearch | Langfuse |
|---|---|---|
| 역할 | 운영 상태·`errorCode`·단계별 지연·토큰 요약·전송 실패 분석의 정본 | Agent 계층, prompt/response, 중간 산출물, 모델 지연·토큰·비용, 품질 평가 |
| 단위 | `taskId` 단위 이벤트 문서 | trace → span/generation 계층 |
| 필수 여부 | 운영 필수 | 선택. 꺼도 Timeline 처리에 영향 없음 |

### 중복 데이터 처리 방침

본문(입력·프롬프트·응답·중간 산출물)을 두 저장소에 동시에 장기 보관하지 않는다.

- 운영 기본값은 Elasticsearch `OBS_CONTENT_CAPTURE=SANITIZED` + Langfuse `NONE`이다.
  이 조합에서는 본문 정본이 Elasticsearch 하나뿐이고 Langfuse에는 진단 지표와
  길이·해시만 남으므로 중복이 생기지 않는다.
- local/dev는 Langfuse도 `SANITIZED`가 기본이다. 개발 중에는 프롬프트와 중간 산출물을
  봐야 디버깅이 되기 때문이며, 이 구간은 의도된 중복이다.
- POC 기간에만 한시적으로 운영에서도 양쪽 `SANITIZED`를 함께 켜서 같은 `taskId`의
  기록을 비교한다. 이때는 중복 구간이므로 비교가 끝나면 되돌린다.
- Langfuse가 Agent 분석 경로로 안정화되어 본문을 Langfuse에서 보게 되면,
  Elasticsearch를 `OBS_CONTENT_CAPTURE=NONE`으로 전환해 본문 정본을 Langfuse로
  옮긴다. Elasticsearch에는 상태·`errorCode`·지연·토큰 같은 메타데이터만 남긴다.
- 어느 방향이든 본문을 보관하는 쪽에만 접근 권한과 보존 기간 정책을 적용한다.
  전환 시점과 보존 기간은 운영 결정 사항이며 이 문서를 갱신해 남긴다.

## 설정

일본 리전 프로젝트의 Settings → API Keys에서 project key pair를 발급한다.
키는 로컬 `.env` 또는 배포 secret에만 넣고 문서, 채팅, 이슈, Git에 기록하지 않는다.

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
LANGFUSE_SAMPLE_RATE=1.0
LANGFUSE_MAX_PAYLOAD_BYTES=65536
```

`LANGFUSE_CONTENT_CAPTURE`는 일부러 비워 둔다. 비워 두면 `APP_ENV` 기준으로
local/dev는 `SANITIZED`, 그 밖은 `NONE`이 적용된다. 값을 적으면 그 값이 항상 이기므로,
dev에서 본문이 안 보이면 **환경 파일에 `LANGFUSE_CONTENT_CAPTURE=NONE`이 남아 있는지
먼저 확인한다**(EC2는 `/opt/laimory-ai/runtime.env`).

- 비활성화되었거나 key pair가 불완전하면 Langfuse는 no-op이다.
- `APP_ENV`는 Langfuse environment, `AGENT_VERSION`은 release/version으로 전파된다.
  EC2 배포는 `scripts/deploy-ec2.sh`가 배포 이미지 태그를 `AGENT_VERSION`으로 넘기므로
  release로 배포본을 특정할 수 있다.
- OTel `service.name`은 `laimory-ai`로 고정한다. 다르게 쓰려면 `OTEL_SERVICE_NAME`을
  지정한다.
- 초기 구조 검수 중에는 `LANGFUSE_SAMPLE_RATE=1.0`을 사용한다. 운영 비율은 비용,
  데이터 양과 조사 요구를 반영해 낮출 수 있다.
- trace 전송·flush 실패는 Timeline 처리, RDB 저장, 콜백과 격리된다.

## 연결 smoke

smoke는 인증, ingestion, observation type, 부모 관계, generation token, `NONE`
마스킹만 검사한다. 실제 Timeline이나 Main Agent를 호출하지 않는다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
$env:LANGFUSE_ENABLED="true"
$env:LANGFUSE_CONTENT_CAPTURE="NONE"
uv run python -m scripts.langfuse_smoke
```

생성되는 trace는 다음처럼 실제 trace와 명확히 구분된다.

- trace: `langfuse-connectivity-smoke`
- root: `verify-langfuse-connectivity` (`chain`)
- tags: `smoke`, `synthetic`

smoke 화면으로 실제 Agent 수나 다중 Agent 계층을 판정하면 안 된다.

## 실제 Timeline 검수 체크리스트

실제 Agent 구조는 비식별 테스트 source snapshot으로 Timeline task 한 건을 완주한 뒤
`generate-timeline` trace에서 검수한다.

운영 DB·App Server에는 접근하지 않고 실제 설정 LLM provider만 호출하는 자동 감사
스크립트도 제공한다. 외부 trace와 LLM 비용이 발생하므로 명시적인 opt-in이 필요하다.

```powershell
$env:LAIMORY_LANGFUSE_AUDIT="1"
$env:UV_CACHE_DIR=".uv-cache"
uv run python -m scripts.langfuse_timeline_audit
```

이 스크립트는 `timeline-audit` environment에서 합성 source를 사용하고, 생성한 trace를
API로 다시 읽어 Agent 계층, 구체적인 generation 이름, input/output, 실제 token usage,
`durationMs`, callback token 비노출을 자동 검사한다.

1. `session:=<taskId>`로 trace를 검색하고 environment와 release가 맞는지 확인한다.
2. trace tree가 위 구조대로 중첩됐는지 확인한다.
3. Agent Graph에서 병렬 Event fan-in과 Repair cycle이 올바르게 표시되는지 확인한다.
4. 모든 주요 단계의 input/output과 `durationMs`가 보이는지 확인한다.
5. 모든 generation에 prompt, 응답, provider, model, 실제 usage가 보이는지 확인한다.
6. root와 Agent output의 `tokenUsage`가 하위 generation 합계와 일치하는지 확인한다.
7. callback token, Secret, 인증 헤더, 사진 bytes가 없는지 검색한다.
8. 같은 `taskId`로 Elasticsearch·CloudWatch 기록과 교차 확인한다.

공식 참고 자료:

- [좋은 trace 구조](https://langfuse.com/docs/observability/best-practices)
- [Agent Graph](https://langfuse.com/docs/observability/features/agent-graphs)
- [Python SDK instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation)
- [토큰과 비용 추적](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
- [마스킹](https://langfuse.com/docs/observability/features/masking)
- [Sampling](https://langfuse.com/docs/observability/features/sampling)
