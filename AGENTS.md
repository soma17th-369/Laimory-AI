# Laimory-AI 에이전트 지침

이 저장소는 FastAPI 기반 Python 서버 프로젝트입니다. Codex와 Claude가 같은 프로젝트 지침을 공유할 수 있도록 이 파일을 공통 기준으로 사용합니다.

## 기본 작업 방식
- 모든 md 파일은 한글을 base 로 생성합니다.
- 변경 전에는 관련 파일을 먼저 읽고 현재 구조를 기준으로 판단합니다.
- 불필요한 리팩터링이나 unrelated 변경은 하지 않습니다.
- 사용자가 명시하지 않은 파일 삭제, git reset, checkout 같은 파괴적 작업은 하지 않습니다.
- 기존 변경사항이 있으면 사용자 작업으로 보고 되돌리지 않습니다.

## Knowledge Workflow

- 구현 전에 [Knowledge Index](.agents/knowledge/README.md)의 Router에서 변경 경로와
  `Read when`이 맞는 문서만 골라 읽습니다. 전체 knowledge를 매번 읽지 않습니다.
- 도메인 이름·필드·모델·API 용어를 만들거나 바꿀 때는
  [공통 언어](.agents/knowledge/domain/ubiquitous-language.md)를 따릅니다.
- 코드 수정 후 변경 경로를 Router의 `Related paths`와 대조하고, 후보 문서의
  `Update when`에 해당하는 의미 변화가 있는지 확인합니다.
- 파일이 바뀌었다는 이유만으로 문서를 갱신하지 않습니다. 계약·동작·불변식·운영
  방식의 의미가 달라진 knowledge 문서만 같은 변경에서 갱신합니다.
- 코드·설정·스키마·테스트·CI workflow가 knowledge 문서보다 우선합니다. 서로
  다르면 권위 원천을 기준으로 구현을 판단하고, 의미가 바뀐 경우 문서를 맞춥니다.
- 새 knowledge 문서는 여러 작업에서 반복해 읽을 가치가 있고 기존 문서의 Scope로
  설명하기 어려울 때만 추가합니다. 작업 로그·session memory·raw note는 넣지 않습니다.
- 실제 secret, credential, token, 사용자 원문은 knowledge에 기록하지 않습니다.

## 이슈·커밋·PR

- commit·push·PR은 사용자가 요청하거나 승인한 경우에만 수행합니다.
- 이슈를 만들거나 제목을 고칠 때는 [이슈 관례](.agents/knowledge/conventions/issue.md)를
  따릅니다. 기존 템플릿과 이력에서 확인된 `아이콘 Type - 한글 요약`
  형식을 사용하고 `🐛 Bug`, `✨ Feature`, `🎨 Refactor`, `🔧 Task` 아이콘을
  생략하지 않습니다. 문서에 없는 Type과 아이콘은 임의로 만들지 않고 확인합니다.
- PR을 작성·검토할 때는 [PR 관례](.agents/knowledge/conventions/pull-request.md)와
  `.github/pull_request_template.md`를 따릅니다.
- PR을 준비할 때는 [커밋 관례](.agents/knowledge/conventions/commit.md)에 따라 변경을
  독립적으로 검토하고 되돌릴 수 있는 작은 작업 단위로 최대한 자세히 나눕니다.
- commit 하나에는 하나의 주된 목적만 두고 unrelated refactor·formatting을 섞지
  않습니다. 다만 code와 필수 test를 억지로 분리해 중간 commit을 실패 상태로 만들지는
  않습니다.
- commit message는 관찰된 `type : 한글 설명` 형식을 따르고, 무엇의 어떤 계약이나
  동작을 바꿨는지 구체적으로 적습니다.

## Python 환경

- Python 버전은 `.python-version`과 `pyproject.toml` 기준을 따릅니다.
- 이 프로젝트는 `uv`와 `.venv`를 사용합니다.
- 의존성 설치는 프로젝트 루트에서 `uv sync`를 사용합니다.
- pytest는 가능하면 `-p no:cacheprovider`로 실행합니다. `--basetemp`나 임시 캐시
  디렉터리를 만들어도 되지만, 검증이 끝나면 해당 작업에서 만든 `.pytest-*`,
  `.test-tmp-*`, `pytest-cache-files-*`를 저장소에 남기지 않고 삭제합니다.
- Windows에서 기본 uv 캐시 권한 문제가 있으면 다음처럼 로컬 캐시를 사용합니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv sync
```

## 실행

FastAPI 앱 진입점은 `app.server:app`입니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv run uvicorn app.server:app --reload
```

## 배포

배포 경로가 브랜치로 갈립니다(#90). `dev` 는 개발, `main` 은 production 의 정본입니다.

| 브랜치 | 워크플로 | 대상 | 아키텍처 | ECR |
|---|---|---|---|---|
| `dev` push | `deploy-ec2.yml` | EC2 단일 컨테이너 | `linux/amd64` | `laimory-ai` |
| `main` push | `deploy-production.yml` | AgentCore Runtime | `linux/arm64` | `laimory-ai-prod` |

`main` 에는 `dev` 에서 온 PR 로만 들어갑니다. merge 되면 production 배포가 승인 대기로
뜨고, 승인해야 AWS 자격증명이 발급됩니다. IAM 배포 역할은 두 경로가 함께 쓰므로
(`AWS_DEPLOY_ROLE_ARN`) **역할이 권한 경계가 아닙니다.** 경계는 ECR 저장소 분리, 승인
게이트, Environment 배포 브랜치 정책, 워크플로별 실행 브랜치 가드가 만듭니다.

- production 승격·배포·롤백과 GitHub·AWS 설정은 [docs/deploy-production.md](docs/deploy-production.md)를 따릅니다(#90).
- EC2(개발) 절차와 AWS 사전 준비는 [docs/deploy-ec2.md](docs/deploy-ec2.md)를 따릅니다.
- AgentCore Runtime 계약과 AWS 자원 준비는 [docs/deploy-agentcore.md](docs/deploy-agentcore.md)를 따릅니다.
- Runtime·엔드포인트를 처음 만들 때 사람이 하는 작업은
  [docs/agentcore-cutover-manual.md](docs/agentcore-cutover-manual.md)를 따릅니다(#89).
- 컨테이너는 8080 포트에서 `POST /v1/timeline`, `POST /invocations`, `GET /ping` 을 제공합니다.
  Host `0.0.0.0`, Port `8080`, **`linux/arm64`** 는 AgentCore Runtime 이 요구하는 값입니다.
- **ECR push 는 배포가 아닙니다.** AgentCore 는 ECR 을 감시하지 않습니다. `UpdateAgentRuntime`
  이 새 Runtime 버전을 만들고, `UpdateAgentRuntimeEndpoint` 로 엔드포인트가 그 버전을
  가리켜야 배포입니다. 롤백은 그 포인터를 되돌리는 것이라 재빌드가 없습니다.
- production 이미지에 이동 태그(`latest`·`dev`)를 붙이지 않습니다. Runtime 버전과 이미지가
  1:1 로 고정돼야 롤백이 성립합니다.
- 두 경로는 ECR 저장소를 공유하지 않습니다. `scripts/prune_ecr_images.py` 가 저장소 전체에서
  EC2 배포 태그가 없는 이미지를 지우므로, 한 저장소를 쓰면 dev 배포가 운영 이미지를 지웁니다.
- uvicorn worker 를 늘리지 않습니다. `app/core/inflight.py` 의 진행 중 처리 카운터가 프로세스 로컬이라 worker 가 여럿이면 `/ping` 이 잘못된 상태를 답합니다.

## 스킬 공유

- Codex용 프로젝트 스킬 원본은 `.agents/skills/` 아래에 둡니다.
- Claude 쪽에서 공유할 때는 `.agents/skills/` 내용을 `.claude/skills/`로 복사해 동기화합니다.
- `.claude/skills/`는 링크가 아니라 복사본이며, 필요할 때 `scripts/link-skills.ps1` 또는 `scripts/link-skills.sh`를 다시 실행해 갱신합니다.

## Project Structure
```
app/
├── server.py                  # FastAPI 앱 생성 + 라우터 등록만 (얇게)
│
├── core/                      # 공통 인프라
│   ├── config.py              # 설정 (pydantic-settings, LLM_PROVIDER/API 키, APP_SERVER_*, OBS_*/ES_* 등)
│   ├── logging.py             # 운영 로그 설정 (rich | stdout JSON→CloudWatch, LOG_FORMAT)
│   ├── error_codes.py         # 오류 코드 카탈로그 (#42). 정수 코드·외부 안전 메시지·HTTP 상태의 유일한 정본. 값 중복은 import 시점에 차단
│   ├── exceptions.py          # AppError 예외 계층(자기 ErrorCode 보유) + report_error: 로그와 관측을 같은 코드로 남기는 유일한 통로
│   ├── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini/Bedrock, 확장형) + LLM 관측/토큰 emit.
│   │                          #   `get_provider` 캐시 키가 `(provider, model)` 이라 인스턴스는 모델당 하나다.
│   │                          #   OpenAI 는 모델별 요청 정책을 갖는다(#108). `_MODEL_PARAMS` 표가
│   │                          #   모델 id prefix 로 `_OpenAIModelParams` 를 고르고, 그 dataclass 가
│   │                          #   **매개변수마다 필드를 따로** 갖는다(`reasoning_effort`,
│   │                          #   `accepts_temperature`). 두 값은 역할이 다르다 — 추론량과 샘플링
│   │                          #   분산이라 한쪽에서 다른 쪽을 파생시키지 않는다. GPT-5 계열 reasoning
│   │                          #   모델이 추론 중 `temperature` 를 거부해 지금은 두 값이 함께 움직이지만,
│   │                          #   그건 모델의 현행 계약이지 두 축이 하나라는 뜻이 아니다(어긋남은 표
│   │                          #   일관성 테스트가 잡는다). **환경변수가 아니라 코드가 소유한다** —
│   │                          #   모델이 어떤 매개변수를 받는지는 배포 환경이 아니라 모델의 성질이다.
│   │                          #   표에 없는 모델은 effort 를 싣지 않고 temperature 를 그대로 보낸다
│   ├── llm_stages.py          # 단계별 모델 티어 (#106). `LLMStage`(호출 지점 10개)·`LLMTier`
│   │                          #   (`FAST`/`QUALITY`)·단계→티어 매핑표의 정본. 해석 순서는
│   │                          #   **`{PROVIDER}_MODEL_{TIER}` > `{PROVIDER}_MODEL`** 이고, 티어를 비우면
│   │                          #   provider 싱글턴을 그대로 재사용해 동작이 예전과 같다.
│   │                          #   **provider 는 전역 하나다** — 단계별 provider 선택은 없다.
│   │                          #   다만 **티어 설정은 provider 별로 갈린다**(bedrock·openai).
│   │                          #   provider 마다 쓸 수 있는 모델이 달라, 공용 티어 하나로 두면
│   │                          #   provider 를 바꾸는 순간 없는 모델 id 를 부른다. gemini 는
│   │                          #   티어 필드가 없어 언제나 `GEMINI_MODEL` 하나를 쓴다.
│   │                          #   티어 이름은 모델의 성질만 가리킨다. 단계 배치는 운영하며 바꾸는
│   │                          #   값이라 이름에 용도를 담으면 배치를 바꾸는 순간 이름이 거짓이 된다.
│   │                          #   `FAST` 에 `photo_describe` 가 있어 **그 티어 모델은 vision 필수**.
│   │                          #   관측에는 싣지 않는다 — Langfuse generation 이 이미 실제 `model` 을
│   │                          #   남기고 단계는 generation 이름이 가른다
│   ├── inflight.py            # 진행 중 백그라운드 처리 카운터 (GET /ping 의 Healthy/HealthyBusy 판단용, 프로세스 로컬)
│   ├── secret_bundle.py       # 시크릿 번들 로딩 + pydantic-settings 소스 (#30). config 보다 먼저
│   │                          #   필요해 app.core 를 import 하지 않는다(순환 방지). 조회 실패는
│   │                          #   붙잡아 두고 로깅 준비 뒤 secrets.prefetch_secrets 가 1408 로 보고한다
│   ├── secrets.py             # 시크릿 조회 진입점 (#30). 우선순위는 **번들 > 환경변수/.env >
│   │                          #   config.py 기본값** 이다 — 정본이 하나여서 env 파일에 옛 값이 남아도
│   │                          #   결과가 같다. 대상 키 목록을 코드가 갖지 않는다.
│   │                          #   dev=EC2 runtime.env+번들, prod=AgentCore+번들, 고정값=코드 기본값
│   └── observability/         # Timeline 실행 관측 (#28). taskId 단일 키, SANITIZED 본문·메타데이터
│       ├── models.py          #   ObservationEvent 계약 (taskId/sequence/stage/token/version)
│       ├── context.py         #   contextvars 로 to_thread 까지 taskId 전파, emit_observation
│       ├── observer.py        #   요청별 Observer: sequence 부여·마스킹·sink 실패 격리
│       ├── redaction.py       #   SANITIZED/NONE 콘텐츠 정책·마스킹·payload 크기 제한
│       ├── sinks.py           #   Null/InMemory(버퍼)/JsonLines/Composite (제품 독립)
│       ├── documents.py       #   이벤트 버퍼 → event 문서 N건(FINAL에 task 집계 포함)
│       ├── elasticsearch.py   #   httpx NDJSON _bulk 전송 (재시도/부분실패/완전격리)
│       └── runtime.py         #   요청별 Observer/buffer 생성 + flush(로컬 + ES)
│
├── api/
│   ├── agentcore.py           # AgentCore Runtime 컨테이너 계약 (POST /invocations, GET /ping).
│   │                          #   진입점이 하나뿐이라 payload 최상위 requestType 이 요청 종류를 말한다(#89).
│   │                          #   TIMELINE→v1/timeline, USER_MEMORY_UPDATE→v1/user-memory 로 위임하는 어댑터고
│   │                          #   처리 구현을 갖지 않는다. requestType 키가 없는 body 는 TIMELINE 으로 감싼다 —
│   │                          #   **영구 계약이지 전환 기간용 임시 호환이 아니다.** payload 필드 모양으로
│   │                          #   종류를 추측하지 않는다(taskId·taskToken 이 양쪽에 다 있어 갈리지 않는다)
│   ├── error_handlers.py      # 전역 예외 처리기 (#42). 검증오류/HTTPException/AppError/미처리 4종을 ErrorResponse 로 통일 + OpenAPI ERROR_RESPONSES
│   └── v1/
│       ├── router.py          # v1 라우터 취합
│       ├── timeline.py        # POST /v1/timeline (taskId+taskToken+dailyRecordId+window 접수 → 202). 상태 조회 없음(상태는 App Server 소유)
│       ├── timeline_testing.py # POST /v1/timeline/test (#102). **테스트 전용 동기 경로.**
│       │                      #   요청 body 는 TimelineInputPayload 를 상속해 입력 조회와 같은
│       │                      #   선언을 쓰고 window 만 필수로 좁힌다. taskId 는 App Server 가
│       │                      #   발행한 값을 받는다(비동기 실행과 로그를 이어 보는 상관키).
│       │                      #   taskToken 만 계약에 없다 — 되부르지 않아 인증할 대상이 없다.
│       │                      #   응답은 결과 저장 요청 구조 그대로이며
│       │                      #   **어디에도 저장하지 않는다**. local/dev 에서만 열린다
│       │                      #   (TIMELINE_TEST_ENABLED 가 우선). 닫히면 404/1003 + OpenAPI 비노출
│       └── user_memory.py     # POST /v1/user-memory (#64). 확정된 하루 타임라인 접수 → 202.
│                              #   dailyTimelines 는 최대 5건. 그 안의 event 수·본문 길이는
│                              #   거절하지 않고 digest 에서 자른다
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── error.py               # 공통 오류 응답 ErrorResponse(errorCode:int, error:str)
│   ├── task.py                # TaskStatus + 완료 콜백 payload(errorCode:int|None, 성공/실패 필드 짝 강제)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 파이프라인 내부 계약
│   ├── timeline_input.py      # 입력 계약 두 겹 (#40, #65, #102).
│   │                          #   TimelineInputPayload 가 **입력 한 벌(taskId 포함) 필드 선언의
│   │                          #   유일한 자리**고, TimelineInputResponse 가 거기에 taskToken 만 더한
│   │                          #   App Server 입력 조회 응답이다. 쪼갠 기준은 'AI 가 App Server 를
│   │                          #   되부를 때 쓰는 값인가' 이며 — taskId 는 App Server 가 발행해 AI 가
│   │                          #   받는 값이라 공통, taskToken 은 되부르는 호출의 인증이라 갈린다 —
│   │                          #   동기 테스트 요청이 payload 를 상속해 같은 선언을 쓴다.
│   │                          #   필드를 손으로 다시 적으면 두 입구가 말없이 갈린다.
│   │                          #   window.startAt/endAt → CollectedSnapshot 변환.
│   │                          #   userMemory 는 원본 dict 로 느슨하게 받고 parse_user_memory() 가
│   │                          #   따로 검증한다 — 여기서 엄격히 선언하면 보조 context 하나가
│   │                          #   응답 전체를 1102 로 죽인다
│   ├── user_memory.py         # 사용자 압축 프로필 v1.0 (#65). 고정 자연어 10필드(각 500자) +
│   │                          #   customAttributes(값 500자, **개수 제한 없음**, #121).
│   │                          #   extra="forbid". prompt_payload() 가 projection 규칙(빈 필드·
│   │                          #   메타데이터 제외, 선언 순서)을 소유한다. 상한은 넓히기만 한다 —
│   │                          #   좁히면 저장된 문서가 1106 으로 흡수돼 프로필을 처음부터 다시 만든다.
│   │                          #   `UserMemoryPatch`(#121)는 v3 세트의 **LLM 출력 계약**이다.
│   │                          #   `changes` 는 **변경 목록**이고 한 건은 `item`(고정 필드 이름 또는
│   │                          #   `customAttributes.<키>`)·`action`(추가·수정·삭제)·`reason`(왜
│   │                          #   바꾸는지)·`text`(그 항목의 전체 문장)다. `apply_to` 가 기존 문서에
│   │                          #   끼워 넣는다. 문서 모양으로
│   │                          #   받지 않는다 — 바꾸지 않는 필드를 null 로 두게 했을 때 모델은 그것을
│   │                          #   "새 문서" 로 읽었다. `수정` 은 바꿔 끼우고, **내용이 있는 항목에 온
│   │                          #   `추가` 는 기존 내용 뒤에 덧붙인다** — 모델이 그 자리에 새 문장만
│   │                          #   담아 내므로 바꿔 끼우면 기존 내용이 사라진다. `reason` 은 모델이
│   │                          #   `text` 를 쓰기 전에 적는 판단 과정이고 **코드는 읽지 않는다**
│   │                          #   (Repair 의 `toolCalls[].reason` 과 같은 자리다).
│   │                          #   이 클래스들의 docstring 은 JSON schema 로 provider 에 나가므로 짧게
│   │                          #   두고 설계 이유는 주석에 적는다. `text` 의 길이 제한은 문서와 같다.
│   │                          #   AI 서버 안의 계약이라 저장·전송 형식이 아니다
│   ├── user_memory_update.py  # 갱신 접수·저장 계약 (#64). dailyTimelines 는 최대 5건이고
│   │                          #   그 안의 events[] 를 **느슨하게** 받는다
│   │                          #   (eventType 자유 문자열, endAt·subtitle·question·memo nullable,
│   │                          #   길이 상한 없음). emotionType 은 사용자가 고른 하루 감정이다(#121).
│   │                          #   하루에 하나이고 enum 으로 좁히지 않는다.
│   │                          #   UserMemoryResultRequest 는 status 에 따라 필드 짝
│   │                          #   (SUCCESS→userMemory / FAILED→errorCode)을 강제한다
│   ├── timeline_result.py     # App Server 결과 저장 요청 계약 (#40, #66). eventType/title/subtitle/
│   │                          #   startAt/endAt/sourceRawIds/question. question 은 event 안에 중첩한다 —
│   │                          #   계약에 clientEventId 가 없어 최상위 목록은 event 를 가리킬 수 없다
│   ├── location.py/calendar.py/health.py/notification.py/photo.py  # 분리된 도메인 항목
│   ├── event_candidate.py     # AI 이벤트 후보 모델
│   ├── timeline_request.py    # 정규화된 요청(main agent 입력)
│   └── timeline.py            # 타임라인 초안/이벤트 스키마
│
├── agents/                    # AI 에이전트
│   ├── base.py                # 공통 에이전트 인터페이스
│   ├── parsing.py             # LLM 호출/프롬프트/응답 파싱 유틸
│   ├── events/                # 데이터별 이벤트 에이전트 (source별 폴더)
│   │   └── base_event_agent.py
│   ├── timeline/timeline_agent.py   # 후보 → 초안 병합 (LLM 병합/파싱까지만)
│   ├── question/question_agent.py   # 확정 event → 회고 유도 질문 (#66). Repair 뒤 배치 호출.
│   │                          #   confidence·sourceRefs·분 단위 시각을 프롬프트에 주지 않는다 —
│   │                          #   주지 않으면 질문에 샐 수 없다. 모든 event 에 하나씩이며,
│   │                          #   빠진 event 는 1회 재요청한다. 길이·형식 검사는 코드가 한다.
│   │                          #   User Memory 를 받는다(#65) — 무엇을 물을지가 아니라 어떻게
│   │                          #   물을지(문체·결)를 고르는 자료다
│   ├── user_memory/user_memory_agent.py  # 갱신된 User Memory 문서 생성 (#64, #121). 반환값은 언제나
│   │                          #   문서 전체다. **모델이 무엇을 출력하는지는 세트마다 다르다** —
│   │                          #   v3 는 변경 목록을 내고(`UserMemoryPatch`) 코드가 기존 문서에 끼워
│   │                          #   넣는다. 목록에 없는 항목은 글자 하나 바뀌지 않는다. v1·v2 는 문서
│   │                          #   전체를 다시 출력한다(`_PATCH_OUTPUT`, 기준은 `uses_legacy_contract`).
│   │                          #   **무엇을 근거로 읽는지도 프롬프트 세트가 정한다.**
│   │                          #   title·subtitle·question 은 우리 AI 가 쓴 문장이고 사용자가 직접
│   │                          #   남긴 것은 memo 와 하루 감정뿐이다.
│   │                          #   v1·v2 는 AI 문장에서 성향을 뽑지 않는다 — 모델이 자기 출력을 읽고
│   │                          #   사용자를 만들어 내는 되먹임이 된다. 성향 계열 5필드의 근거는 memo
│   │                          #   뿐이고, memo 없는 날은 그 필드가 그대로인 것이 정상이다.
│   │                          #   v3 는 AI 문장도 근거로 읽는다(아래 「User Memory v3」).
│   │                          #   코드에 남은 근거 정책은 `_MEMO_ONLY_TRAITS` 하나다. v1·v2 에서만
│   │                          #   memo 없는 날 `[근거 없음]` 지시를 붙인다.
│   │                          #   타임라인 파이프라인 밖이라 base.Agent 를 상속하지 않는다
│   └── main/main_agent.py     # events → timeline → repair → question 조율(LangGraph)
│
└── services/
    ├── app_server_client.py   # App Server 서버간 API 클라이언트 (#40, #64). 입력 조회/결과 저장/콜백/
    │                          #   User Memory 결과 저장 4종을 소유.
    │                          #   TaskToken 홀더(응답 body 로 갱신, Task-Token 헤더로만 전송, 로그 금지),
    │                          #   재시도(timeout·5xx)와 중단(401/404/409) 정책의 유일한 자리
    ├── source_contract.py     # 입력 조회 응답의 묶음 계약 검증 (taskId 일치/0건/rawId 중복) + SourceBatchError.
    │                          #   resolve_user_memory 도 여기 있다(#102) — userMemory 계약 위반
    │                          #   흡수(1106)를 비동기·동기 두 경로가 같이 쓴다
    ├── timeline_result.py     # TimelineDraft → 결과 저장 요청 변환 (subtitle←description, question 그대로,
    │                          #   rawId 디듀프, 255자 절단, tz 정렬)
    ├── timeline_validator.py  # 저장 전 자체검증 (task source 소속/시간 등)
    ├── normalizer.py          # 수집 스냅샷을 itemType별로 분리·정규화
    ├── draft_repair.py        # draft 확정 repair (아래 순서대로 조립)
    ├── confirm_report.py      # 확정 pass 의 보정 내역 기록 (#119). guard 를 하나씩 돌릴 때마다
    │                          #   직전·직후의 event 를 비교해 **어느 event 의 어느 값이 무엇에서
    │                          #   무엇으로 바뀌었는지** 적는다. guard 는 고치지 않는다. 지워지거나
    │                          #   합쳐진 event 는 전체 내용을 남긴다 — warning 에 제목 세 개나
    │                          #   건수로만 남으면 빠진 내용을 Repair 가 알 수 없다. 기록은 draft 가
    │                          #   아니라 RepairContext 가 들고 있어 결과 저장 계약에 나가지 않는다
    ├── draft_edit.py          # event 수정·삭제·나누기 (Repair 계획의 결정론 적용).
    │                          #   `split_event`(#119)는 조각의 시간·타입·문장을 호출자에게 받고
    │                          #   **근거는 코드가 원본 시각을 보고 나눠 담는다** — 호출자에게 맡기면
    │                          #   빠뜨린다. 시점 근거(사진·알림)는 조각 하나에만, 구간 근거(체류·
    │                          #   일정)는 충분히 겹치는 조각 모두에 담는다. 원래 event 의 시간
    │                          #   밖으로 걸친 조각은 **거절하지 않고 안쪽만 남긴다** — 거절하자
    │                          #   실제 LLM 이 같은 호출을 세 번 되풀이했다. 구간 근거를 담을 조각이
    │                          #   없으면 거절한다(그 시간의 체류가 조용히 사라지지 않게)
    ├── validator.py           # 요청 시간 범위(window) 강제: 범위 밖 event 제거/경고
    ├── source_lookup.py       # sourceRef → 입력 항목 역참조. rawId가 정식 식별자이고,
    │                          #   LLM이 붙인 sourceType 라벨은 입력의 실제 타입으로 정정한다
    ├── sleep_guard.py         # 수면 경계 강제: 기상 이전 event 제거, 수면에 걸친 event 클램프
    ├── stay_merge.py          # 이동 없이 이어진 같은 장소 STAY 묶기 (끊긴 수집 복원, 입력만 분석)
    ├── calendar_guard.py      # timeline 에서 통째로 빠진 캘린더 일정을 event 로 복원 (누락 방지)
    ├── calendar_location.py    # 캘린더 locationText ↔ STAY place/address 일치 시 confidence 보강
    ├── meal_guard.py           # MEAL event 지속시간 20~60분 강제 (긴 체류 전체를 식사로 잡지 않음).
    │                           #   음식 사진·결제 알림 같은 시점 근거가 없는 식사는 길이와
    │                           #   무관하게 confidence 를 0.6 이하로 묶는다(#118)
    ├── narrative_guard.py      # 사용자 노출 description 길이 검사 (#61). 120자 초과를 LOW
    │                           #   warning 으로 남긴다. 문체·문장 수는 재지 않는다(의미 판단)
    ├── duration_guard.py       # eventType 별 지속시간 상한 검사 (#61, #119). 상한의 정본은
    │                           #   `DURATION_LIMITS` 표 하나다. **모든 eventType 을 한 줄씩 적고
    │                           #   기본값을 두지 않는다** — 값을 바꿀 때 그 줄만 고치면 되고, 새
    │                           #   종류는 표에 적지 않으면 import 에서 멈춘다. Timeline v3 프롬프트와
    │                           #   `docs/ai-event-candidate.md` 표가 같은 값을 말하는지는 테스트가
    │                           #   본다. 초과를 LOW warning 으로 남기고 **자르거나 나누지 않는다** —
    │                           #   어디서 끊을지는 Repair 의 판단이다. 값이 `None` 인 종류는 재지
    │                           #   않는다(지속 구간이 근거에 직접 있거나 다른 guard 담당).
    │                           #   **캘린더 근거가 있고 event 길이가 그 일정의 길이를 넘지 않으면
    │                           #   타입과 무관하게 면제**하고,
    │                           #   MOVEMENT 근거가 있는 EXERCISE(산책)도 면제한다. **코드가 하나로
    │                           #   합치는 체류(근거가 전부 한 묶음의 STAY)도 면제한다** — 나눠도
    │                           #   `merge_stay_events` 가 다음 확정에서 다시 합쳐 Repair 가 고칠 수 없다.
    │                           #   v1·v2 세트에서는 예전처럼 일괄 3시간이고 면제도 예전 그대로다
    ├── movement_stay_guard.py  # 이동 사이에 낀 장시간 체류 검사 (#119). 하나의 candidate·event 가
    │                           #   `MOVEMENT → 20분 초과 STAY → MOVEMENT` 를 함께 품으면 위반이다.
    │                           #   기준은 `location_metrics.SHORT_STAY_MAX` 하나다. **20분을 넘으면
    │                           #   환승·대기라도 예외가 없다.** eventType 을 보지 않는다(MOVEMENT 도
    │                           #   검사한다). **찾기만 하고 나누지 않는다** — 걸린 체류와 나눌 자리
    │                           #   (`segments`)를 넘기고 나누는 것은 Repair 가 한다. 나눌 자리는
    │                           #   **event 의 시간 안으로 맞춘 값**이다(요청 시간 범위 끝에서 잘린
    │                           #   event 는 근거가 그 뒤까지 이어진다). **수집이 끊기지 않고 이어진
    │                           #   근거끼리만 하나의 이동으로 묶는다**(`COVERAGE_GAP_MIN`). 이동과
    │                           #   이어지지 않은 20분 이하 체류는 나눌 자리가 아니라 `shortStays`
    │                           #   로 따로 알린다. v3 세트에서만 돈다
    ├── conversation_guard.py   # 대화로 만든 event 개수 제한 (#119). 하루 최대 3개. 근거가 전부
    │                           #   알림이고 타입이 SOCIAL·WORK·MEETING 인 event 를 센다. **메신저
    │                           #   정책이 있는 앱의 알림만 대화로 세고, 사전에 없는 앱은 세지도
    │                           #   지우지도 않는다** — Notification Agent 와 같은 구분이다.
    │                           #   **넘으면 알림이 많은 3개만 남기고 코드가 지운다.** 기준이 알림
    │                           #   수 하나라 LLM 이 판단할 것이 없다. Repair 에 맡겼을 때 실제 LLM 은
    │                           #   알림 수 순서를 4번 중 3번 따르지 않았다. v3 세트에서만 돈다
    ├── photo_guard.py          # 사진 단일 귀속. **검출이 아니라 강제다(#119).** 입력의 모든 사진이
    │                           #   정확히 한 event 에만 있게 만들고 어떤 입력에서도 실패하지 않는다.
    │                           #   중복은 사진 말고 다른 근거가 있는 event → 촬영 시각을 포함하는
    │                           #   event → 가장 짧은 event 순으로 하나만 남긴다. 사진을 빼서 근거가
    │                           #   남지 않는 event(사진만으로 만든 PHOTO_MOMENT)는 지운다. 누락은
    │                           #   촬영 시각 기준으로 붙이고, event 가 하나도 없으면 PHOTO_MOMENT 를
    │                           #   만든다. 한 event 에 사진 여러 장(N:1)은 그대로 허용한다
    ├── event_count_guard.py    # 최종 event 개수 상한 검사 (#118). 10개 초과를 MEDIUM warning 으로
    │                           #   남기고 **자르지 않는다** — 무엇을 합칠지는 의미 판단이라 코드가
    │                           #   고르면 캘린더·사진 근거를 잃는다. 반복마다 다시 잰다.
    │                           #   10개는 v3 프롬프트가 지시하는 값이다. **v1·v2 세트는 예전 값
    │                           #   24개로 잰다** — 그 Timeline 프롬프트에는 개수 지시가 없다
    ├── place_resolver.py       # 장소 확정의 유일한 자리. 우선순위(STAY→MOVEMENT→PHOTO→CALENDAR)를
    │                          #   `_PLACE_SOURCES` 목록 하나가 소유한다. 세 가지 일을 한다.
    │                          #   (1) resolve_candidate_places (#72): candidate 의 places/
    │                          #       address 를 sourceRefs 로 찾은 입력에서 **그대로
    │                          #       복사**한다. 단수 place 를 두지 않는다 — 복수는 고를
    │                          #       후보(입력), 단수는 고른 결과(출력 place)다. Event Agent 는 채우지 않는다 — 한 지점에
    │                          #       이름이 여럿일 때 어느 것이 맞는지 판단할 근거가 없다.
    │                          #       places 를 줄이지 않는 이유도 그것이다(고르는 건 Timeline)
    │                          #   (2) place 를 근거 place 로 확정, 근거 없는 address 제거
    │                          #   (3) 보존 검사 (#72): Timeline 이 쓴 place 가 입력이나
    │                          #       User Memory 에 있는지 본다. 없으면 LOW warning 만 남기고
    │                          #       **지우지 않는다**. address 만 지운다
    ├── place_text.py           # 장소 문자열 정규화·비교 (calendar_location/place_resolver/stay_merge 공용)
    ├── timeline_runner.py     # 백그라운드(무상태): 입력 조회→정규화→main agent→결과 저장→콜백. 최종 상태 반환
    ├── timeline_testing.py    # 동기(무상태·무저장) (#102): 계약 검증→정규화→main agent→자체검증→
    │                          #   결과 계약 변환. **App Server 를 부르지 않는다.** 제한 시간과
    │                          #   초과 처리(#76), execution_context, track_inflight 는 runner 와
    │                          #   같게 쓰고 저장·콜백·토큰 코드는 갖지 않는다
    ├── user_memory_limits.py  # 갱신 크기 정책 (#64, #121). **v3 는 규칙을 어긴 변경만 뺀다**
    │                          #   (`apply_changes`). 없는 항목·빈 문장·항목 길이 초과·민감한 값·
    │                          #   지우기만 하는 변경·적용하면 전체 상한을 넘는 변경이 그것이고, 그
    │                          #   항목은 기존 내용 그대로 남는다. 예전에는 변경 하나가 어기면 목록
    │                          #   전체를 다시 요청하거나 갱신 전체를 1304 로 버렸다. 전체 상한은
    │                          #   문서를 키우지 않는 변경부터 적용하고, 키우는 변경을 목록 순서대로
    │                          #   자리가 남는 데까지 넣어 지킨다. 뺀 것은 이유별 개수만 로그에 남긴다.
    │                          #   다시 요청하지 않으므로 모델은 길이를 넘겼다는 것을 알 수 없다 —
    │                          #   제한에 가까운 항목의 크기를 `[크기]` 절이 미리 알린다
    │                          #   (`items_near_limit`, v3 세트만). **기존 내용을 지우기만 하는 변경을
    │                          #   적용하지 않는다**(`drop_removals`). `삭제` 와, 기존 문장 몇 개를
    │                          #   빼기만 한 `수정` 이 그것이다. 모델이 "이번 기록에 없다" 는 이유로
    │                          #   있던 속성을 지웠고 프롬프트가 금지해도 어겼다. 예외는 크기다 —
    │                          #   기존 문서가 목표를 넘었을 때 **줄일 몫을 받은 항목만** 지울 수
    │                          #   있고, 속성을 통째로 지우는 것은 속성 수의 몫이 나갔을 때만이다.
    │                          #   고쳐 쓰면서 내용을 빠뜨리는 것은 코드가 잡지 못한다.
    │                          #   dailyTimelines 는 schema 에서 최대 5건,
    │                          #   그 안의 입력은 **거절하지 않고 자른다**(하루당 event 10개,
    │                          #   memo 있는 event 우선 보존). 10개는 Timeline v3 의 하루 event
    │                          #   상한과 같은 값이고 테스트가 둘을 묶는다. digest 는 날짜마다 하루 감정
    │                          #   (`emotion`)을, event 마다 시 단위 시작·끝(`hour`·`endHour`)을
    │                          #   싣고 분 단위 시각과 question 은 싣지 않는다. 출력은 **자르지 않고
    │                          #   지적한다**(전체 2,000자·민감정보). 지적 문장에 값을 인용하지 않고
    │                          #   **무엇부터 줄일지도 적지 않는다** — 그것은 프롬프트 세트의 정책이다.
    │                          #   customAttributes 개수 제한이 없어 끝을 막는 값은 전체 상한 하나다.
    │                          #   **거절 기준(2,000자)과 모델에게 알려 주는 목표(1,600자)가 따로
    │                          #   있다.** 모델은 글자 수를 세지 못해 상한을 겨냥하게 하면 넘긴다.
    │                          #   **줄일 몫은 넘은 만큼만이다.** 덜어 낼 문장을 항목의 문장 수에
    │                          #   비례해 나누고, 몫을 받은 항목만 `[크기]` 에 적는다. 예전에는
    │                          #   비율을 항목마다 내림해 일곱 자를 넘었을 뿐인데 485자가 지워졌다.
    │                          #   **얼마나 줄일지는 항목별 문장 수로 준다**(`shrink_budget`) —
    │                          #   글자 수로 주면 모델이 따르지 않는다(실측: 전체 글자 수 1%,
    │                          #   항목별 글자 수 5%, 항목별 문장 수 14% 감소)
    ├── user_memory_repair.py  # 갱신본 확정 (#64). 크기·민감정보를 어기면 문서를 만들지 않는다
    │                          #   (1304). v3 세트는 어긴 변경만 빼므로 여기까지 오는 것은 받은
    │                          #   문서가 이미 규칙을 어긴 경우뿐이다. 문서 전체를 받는 v1·v2 는
    │                          #   지금처럼 여기서 걸린다.
    │                          #   **다시 요청하지 않는다**(#121) — 갱신 한 건은 LLM 호출 한 번이고,
    │                          #   다시 시도하는 것은 App Server 의 다음 배치다. 지적을 붙여 다시
    │                          #   묻던 경로(`MAX_REPAIR_ATTEMPTS`)는 걷어냈다. schemaVersion·
    │                          #   updatedAt 은 서버가 박는다. 모듈 이름의 repair 는 그때의 이름이다
    └── user_memory_runner.py  # 백그라운드(무상태): 기존 프로필 해석→digest→Agent→확정→**결과 저장
                               #   1회**. 모든 실패 경로가 그 한 번으로 수렴해야 한다

# User Memory 갱신 흐름(#64): taskId+taskToken+userMemory+dailyTimelines 접수 → 202 즉시응답 →
#   (백그라운드) 기존 프로필 해석(실패는 1106 으로 흡수하고 새로 만든다) → digest(자르기)
#   → 갱신 Agent → 크기·민감정보 확정 → **결과 저장 1회**
#   **콜백이 없다.** 결과 저장 한 번이 결과 전달과 종료 통보를 겸하며 성공·실패가 같은
#   경로로 나간다. 순서 계약도 토큰 갱신도 없다(호출이 하나라 그럴 기회가 없다).
#   어떤 실패 경로에서도 이 호출을 빠뜨리면 App Server 작업이 TTL 까지 매달린다.
#   **`FAILED` 는 "User Memory 가 안 바뀌었다"는 뜻이지 "하루 기록 저장이 실패했다"가
#   아니다.** DailyRecord 의 DRAFT→SAVED 전이는 앱→App Server 구간에서 이미 끝나 있고,
#   둘을 묶으면 AI 실패가 사용자의 일기 저장을 되돌린다.
#   `user_memory_timeout_sec`(기본 120초)로 감싼다 — llm.py 에 자체 timeout 이 없어
#   상한이 없으면 한 작업이 10분 매달리고 그동안 /ping 이 HealthyBusy 라 배포가 막힌다.
# User Memory v3(#121): **v3 프롬프트만 새 정책이고 v1·v2 파일은 그대로다**(v1 과 v2 는 서로
#   같아야 한다). v3 는 `title`·`subtitle` 도 근거로 읽는다 — 사용자가 읽고 저장한 기록이라
#   받아들인 내용으로 본다. 폭넓게 모으고 타임라인 쓸모로 거르지 않으며, **반복 여부를 저장
#   조건으로 삼지 않는다.** 갱신 Agent 는 이번 기록만 보고 반복을 알 수 없고, 적어 둔 것이
#   있어야 다음 기록에서 반복임을 알 수 있다. 한 번 있던 일은 한 번 있던 일로, 추론은
#   `~로 보입니다` 로 구분해 적는다. 겹치면 합치고, 새 내용은 더하고, 충돌하면 새 정보로 바꾼다.
#   되먹임(AI 가 쓴 문장을 읽고 사용자를 만들어 내는 것)은 없어지지 않는다. 프롬프트가 문장의
#   말투가 아니라 사실을 읽게 하고, memo·감정과 어긋나면 사용자가 직접 남긴 쪽을 따르게 해 줄인다.
#   사람 이름과 장소 이름은 남길 수 있다 — Timeline v3 의 4단계가 `relationships` 의 호칭과
#   생활 장소명을 전제한다. 연락처·금융·인증값과 상세 주소는 계속 금지다.
#   하루 감정(`emotionType`)은 App Server 가 5단계 값으로 보낸다. 하루에 하나이고 event 별
#   감정은 없다. `eventType`(활동 종류)과 다른 값이다.
#   **v3 의 모델은 문서 전체를 다시 쓰지 않고 변경 목록을 낸다.** 어느 항목을 추가·수정·
#   삭제할지와 그 항목의 새 문장이다. 코드가 그것을 기존 문서에 끼워 넣으므로 목록에 없는
#   항목은 글자 하나 바뀌지 않는다. 전체를 다시 쓰게 하면 건드릴 이유가 없던 항목까지 조금씩
#   달라지거나 빠진다(실측에서 "사는 곳" 이 줄이는 과정에서 사라졌다). 바꾸는 단위는
#   항목(고정 필드 하나·속성 하나)이고 새 값은 그 항목의 전체 문장이다. **App Server 로
#   나가는 것은 여전히 문서 전체**라 서버간 계약은 그대로다.
#   기존 문서가 없으면 빈 문서에 끼워 넣는다 — 없는 것과 비어 있는 것을 가르지 않는다.
#   **변경마다 `reason` 을 `text` 보다 먼저 적게 한다.** `견준 결과: 이번 기록의 사실` 한
#   문장이고, 견준 결과는 새로움·합침·반복·충돌 중 하나다(`이미 있음` 이면 변경이 아니다).
#   추론이 꺼진 모델은 출력 말고는 생각할 자리가 없다. 이유 없이 변경만 받았을 때 실제
#   모델은 알게 된 것 하나를 여러 항목에 되풀이해 적고, 주제가 다른 내용을 있던 속성에 몰아
#   적고, 바꿀 것이 없는 항목을 같은 문장으로 다시 냈다. 코드는 `reason` 을 읽지 않고
#   비어 있어도 거절하지 않는다. 이유를 적는다고 잘못된 변경이 없어지지는 않는다 — 모델이
#   이유를 지어내기도 한다(실측: 30일치에 속성 몰아 적기 1건, 필요 없는 삭제 1건).
#   **v3 프롬프트는 작업 순서대로 위에서 아래로 읽힌다.** 기록 읽기 → 항목별 추론 → 변경
#   결정 → 문장 작성 → 최종 검증의 다섯 단계이고, 1단계에서는 기존 프로필을 쓰지 않는다
#   (기록의 빈 곳을 프로필로 채워 읽으면 프로필이 자기 내용을 확인해 준다). 단계의 규칙은
#   **기본값**이며 항목의 절이 다르게 적으면 그 항목에서는 그 절이 이긴다. 항목 하나를 채우는
#   데 필요한 것(담는 것·읽는 근거·추론·갱신·근거가 약할 때·예시·피할 문장)은 **그 항목의 절
#   하나**에 모여 있고, 헷갈리는 항목의 경계는 따로 둔다. 프로필에 적는 것은 그날 있던 일이
#   아니라 그 일이 사용자에 대해 알려 주는 것이다 — 그날의 업무·식사·이동은 옮겨 적지 않는다.
#   하루의 예외(본가 방문, 여행)는 충돌이 아니고, 충돌이면 옛 정보가 적힌 모든 항목에서 고친다.
#   **기존 내용은 지우지 않는다.** 달라졌으면 달라진 내용으로 고쳐 쓰고(취소한 계획은
#   `취소했습니다` 로), 이번 기록에 나오지 않았으면 그대로 둔다. 프로필은 여러 날에 걸쳐
#   쌓은 것이라 하루의 기록에 다 나오지 않는다. 프롬프트가 그렇게 말하고, 지우기만 하는
#   변경은 코드가 적용하지 않는다. 지울 수 있는 것은 목표 크기를 넘어 줄여야 할 때, 줄일
#   몫을 받은 항목뿐이다.
#   `customAttributes` 는 열 필드 어느 것의 정의에도 맞지 않는 정보의 자리다. 필드에 적은
#   것을 속성에 또 적지 않는다(실측: 부모님이 사는 곳이 `relationships` 와 속성 양쪽에).
#   **상한에 닿은 뒤가 평소다.** 보존 정책 아래에서 프로필은 며칠이면 상한에 닿고 그 뒤로는
#   매일 상한 근처에서 갱신된다. 문서 전체를 다시 쓸 때는 한 번 상한을 넘겨 실패하면 프로필이
#   그대로 남아 다음 날도 같은 자리에서 시작했다(실측: 상한만 알려 줬을 때 닷새 중 나흘 실패).
#   그래서 갱신 요청의 `[크기]` 절이 기존 프로필의 크기와 목표를 알리고, 목표를 넘었으면
#   **새 정보를 얹기 전에 먼저 줄일 몫**을 항목별 문장 수로 준다. 크기는 남기는 규칙보다
#   앞서고, 줄일 때도 사는 곳·직업·나이·성별·사람의 이름과 관계는 끝까지 남긴다.
#   v3 에서는 **줄이는 항목도 변경 목록에 담아야 줄어든다.**
#   **다시 요청하지 않고, v3 는 어긴 변경만 뺀다.** 갱신 한 건은 LLM 호출 한 번이다. 변경
#   하나가 항목 길이를 넘거나, 민감한 값을 담거나, 적용하면 문서가 상한을 넘으면 그 변경만
#   빠지고 그 항목은 기존 내용 그대로 남는다. 나머지 변경은 적용된다. 그날의 갱신을 통째로
#   버리지 않는다. 문서 전체를 받는 v1·v2 는 항목을 가려 뺄 수 없어 지금처럼 1304 로 끝난다.
#   빠진 변경은 다시 쓸 기회가 없으므로 `[크기]` 절이 줄일 몫과 제한에 가까운 항목을 미리 알린다.
#   **버전을 가리지 않는 것**: 스키마 상한, 전체 상한과 목표, digest(감정·`endHour`·하루 10개),
#   `[크기]` 절, 재요청 없음. **버전으로 갈리는 것**: 근거 정책(`[근거 없음]` 지시)과 모델
#   출력의 모양(변경 목록 / 문서 전체), 그리고 그 모양에서 나오는 것 — 어겼을 때 변경만
#   빼는지 갱신 전체를 버리는지. 둘 다 `uses_legacy_contract()` 하나로 가른다.
#   v1·v2 프롬프트는 `emotion`·`endHour` 를 설명하지 않지만 입력에는 실린다.
#   v3 프롬프트가 말하는 숫자·감정 값·입력 키·동작 세 가지·출력 예시는 코드와 같아야 하고
#   절의 순서와 항목의 절이 갖는 것도 테스트가 본다.
#   필드의 정의는 「항목별 규칙」에서 그 항목의 **담는 것** 한 줄이다. 그것을 고치면
#   Timeline v3 의 「user memory가 말하는 것」과 Repair v3 의 「User Memory 반영」도 같은
#   문장으로 고친다. 두 곳 모두 테스트가 쓰는 쪽과 대조한다.
# 처리 흐름: taskId+taskToken+dailyRecordId+window 접수 → 202 즉시응답 →
#   (백그라운드) 입력 조회 API → 요청 window 를 정본으로 덮어쓰기 → normalize → main agent
#   → 저장 전 자체검증 → 결과 저장 API(200 확인) → 콜백(SUCCESS/FAILED 통보만)
# 제한 시간(#76): main agent 는 `pipeline_timeout_sec`(120초) 로 감싼다. **timeout 그
#   자체는 실패가 아니다.** Repair 가 draft 를 확정할 때마다(`_confirm`) 복사본을 runner 로
#   발행하므로, 제한 시간이 끝나 실행이 취소돼도 마지막 확정본이 남는다. 그것이 있으면
#   평소와 같은 저장 경로를 그대로 지나 SUCCESS 로 끝내고, 하나도 없을 때만 1201 로 실패한다.
#   부분 저장은 `timedOut`·`partialSave` 로 구분하고 errorCode 는 비운다 — 성공한 작업에
#   실패 코드를 붙이면 지연 감시가 실제 실패와 섞인다.
#   발행 값은 **참조가 아니라 deep copy** 다. `asyncio.wait_for` 는 코루틴만 취소하고
#   `asyncio.to_thread` 위의 LLM 호출은 못 끊어, 취소 뒤에도 그 스레드가 draft 를 마저 고친다.
# 토큰(#40): 작업 하나에 taskToken 하나. 최초 값은 접수 요청 body, 이후는 App Server 응답
#   body 의 taskToken 으로 갱신한다. 인증은 언제나 Task-Token 헤더다. 파생·교체하지 않고
#   로그·관측에 값을 남기지 않는다(갱신 횟수만 남긴다).
# 순서 계약(#40): 결과 저장 200 을 확인한 뒤에만 SUCCESS 콜백을 보낸다. 저장 성공 후에는
#   어떤 이유로도 FAILED 를 보내지 않는다. 401/404/409 는 콜백도 거절되므로 통보 없이 중단한다.
#   timeout/5xx 는 같은 토큰·같은 body 로 재시도한다.
# 오류 계약(#42): 모든 실패는 정수 errorCode 하나로 식별한다. API 응답·콜백·운영 로그·
#   관측 이벤트가 같은 코드를 쓴다. 코드 정본은 app/core/error_codes.py, 표와 연동 방법은
#   docs/error-codes.md. except 블록은 report_error 만 호출한다(로그+관측 동시 기록).
#   error 문자열에는 카탈로그의 안전 메시지만 나가고 원본 예외 메시지는 로그에만 남는다.
#   관측 모듈 자신의 실패는 emit=False (관측으로 알리면 같은 경로를 다시 타 재귀한다).
# AI 서버는 무상태다. task 상태는 App Server 가 소유하며(AI 는 상태 저장/조회 없음),
#   AI 는 상태를 콜백으로만 통보한다.
# 데이터 접근 경계(#40): AI 서버는 DB 에 직접 접근하지 않는다. 수집 원본 조회도 결과 저장도
#   App Server API 로만 한다. DB 모듈·드라이버·접속 설정은 제거됐고, 되돌리지 않는다.
#   APP_SERVER_API_URL 은 필수 설정이다(없으면 기동 실패). dailyRecordId 는 접수 요청에
#   남아 있지만 저장 연결은 App Server 담당이라 AI 는 관측 상관값으로만 쓴다.
# main agent 그래프: run_event_agents → merge_results → run_timeline_agent → repair_draft
#   → run_question_agent
#   merge_results 는 취합만 하지 않는다. `merge_event_results` 가 candidate 의 places/address 를
#   입력에서 복사한다(#72). 이 자리인 이유는 Timeline Agent 로 들어가는 fan-in 이
#   여기 하나뿐이기 때문이다 — Repair 의 `rerun_timeline_agent` 도 같은 함수를 지나므로
#   최초 실행과 재실행이 같은 입력을 본다.
#   앞 3개는 LLM 이 의미를 판단하는 확률적 단계, repair_draft 는 코드가 확정하는 결정론적 단계다.
#   main agent 는 draft 를 돌려주기 직전에 사진 단일 귀속을 한 번 더 강제한다(#119).
# repair_draft 순서: sourceType 정정 → 캘린더 복원 → duration → 근거 구간 정렬 → MEAL
#   → 수면 경계 → window → 장소 확정 → 정렬 → 체류 병합 → 겹침 정리 → **사진 단일 귀속**
#   → confidence 보강 → **대화 개수 제한(v3 세트)** → 검사(사진·알림 안전성, 문장 길이,
#   타입별 지속시간, event 개수(10), 이동 사이 장시간 체류) → clientEventId 재부여
#   검사는 맨 뒤여야 한다. 병합·겹침 정리로 문장·시간·개수가 바뀌므로 앞에 두면
#   곧 사라질 값을 재게 된다. 자기 warning 을 가진 검사는 Repair 반복마다 이전 것을 지우고 다시 잰다.
#   사진 단일 귀속이 겹침 정리 뒤인 것도 같은 이유다 — event 를 지우거나 합치는 단계가 모두
#   끝난 뒤여야 한다.
# 고치는 것과 찾는 것(#119): **무엇을 고칠지가 규칙으로 정해져 있으면 코드가 고치고, 어디서
#   끊고 무엇을 남길지가 의미 판단이면 찾아서 Repair 에 넘긴다.** 이동 사이의 장시간 체류와
#   상한을 넘긴 event 를 나누는 것은 뒤쪽이다 — 나눈 조각마다 무엇을 했는지 다시 써야 한다.
#   그래서 **Repair 가 고치지 못하면(LLM 실패, 제한 시간, 반복 소진) 위반은 warning 과 함께
#   그대로 저장된다.** 코드가 강제하는 것은 둘이다. 사진 단일 귀속은 사용자가 직접 고른
#   입력이라 사라지거나 두 번 보이면 안 된다. 대화 event 하루 3개는 기준이 알림 수 하나라
#   코드가 고를 수 있다.
# #119 의 Repair 계약은 v3 세트에서만 돈다: 확정 pass 의 새 검사(이동 사이 장시간 체류,
#   타입별 지속시간)와 대화 개수 제한, Repair 의 새 입력, `split_event` 도구가 한 묶음이다. 갈리는
#   기준은 `prompt_loader.uses_legacy_contract` 하나다(v1·v2 가 예전 계약). **v2 는 운영
#   세트라 프롬프트가 설명하지 않는 warning·입력·도구를 코드가 먼저 주지 않는다.** 실제 LLM
#   으로 확인한 것이다 — `split_event` 를 v2 에 주자 event 가 7개에서 13개로 쪼개졌고, 새
#   검사의 warning 을 보이자 나눌 도구가 없는 v2 가 Timeline 재실행을 두 번 불렀다.
#   **사진 단일 귀속은 어느 세트에서든 강제한다.** 대화 개수 제한은 v3 가 정한 규칙이라
#   v3 세트에서만 강제한다 — v1·v2 프롬프트에는 대화 개수 지시가 없다.
# Repair 입력(#119): v3 세트에서 Repair 는 `[자동 검사 결과]`(코드가 고친 것·지운 것·찾은 것),
#   `[event 근거]`(event 가 참조한 rawId 의 candidate·fragment), `[user memory]` 를 받는다.
#   프롬프트는 반복마다 그 시점의 draft 로 새로 만든다. **찾은 것은 확정할 때마다 다시 계산하고
#   고친 것은 몇 번째 확정에서 나온 것인지 붙여 쌓는다** — 다시 계산하지 않으면 이미 해소한
#   위반을 또 고치려 들고, 쌓지 않으면 첫 확정에서 고친 event 의 문장을 다듬을 기회가 한 번뿐이다.
#   **v1·v2 세트의 Repair 입력은 예전 그대로다.** v2 프롬프트는 이 입력을 설명하지 않고 v2 는
#   운영 세트라 고치지 않는다.
# Repair v3(#119): 코드가 확정한 뒤에 내용과 문장을 다듬는다. 코드가 확정한 값은 다시 검증하지
#   않는다. **프롬프트는 코드가 무엇을 검사하는지 나열하지 않고, 확정 결과(`[자동 검사 결과]`)를
#   읽는 법만 적는다. 같은 규칙은 한 곳에만 적는다**(테스트가 본다). 작업 순서는 코드가 찾은 것
#   해소 → 코드가 고친
#   event 다시 쓰기 → 내용이 부족한 event 구체화 → 문장 다듬기다. candidate·fragment 에 글자
#   그대로 없어도 **합리적으로 추론되는 사람·장소·활동·목적을 허용**하고 `INFERRED` 로 둔다.
#   Timeline 의 추론을 되돌리지 않게 하는 규칙이 함께 있다 — Timeline 이 쓴 구체적인 이름을
#   `업무` 같은 말로 뭉개지 않고, warning 을 "해소"와 "검토만"으로 나눠 읽고(뒤쪽은
#   추론을 지우라는 뜻이 아니다), 나누라는 검사가 합치라는 warning 보다 먼저이고, 재실행은
#   마지막 수단이다(재실행한 Agent 는 검사 결과를 받지 못한다). v2 는 그대로다.
# 결과 문장 계약(#61): title·description 은 사용자가 읽는 일기다. 1인칭 해요체 과거형,
#   description 1~2문장 100자 내외, title 30자 이내 명사구. 추정 표현(`듯해요`)과 원본
#   수치(분 단위 시각·걸음 수)를 문장에 쓰지 않는다 — 모르는 것은 헤지하지 말고 문장에서
#   뺀다. 불확실성은 confidence·inferenceLevel·uncertainty 가 담당한다.
#   **이 규칙은 Timeline·Repair 에만 적용한다.** Event Agent 는 정확한 사실 보고가 임무라
#   시각·수치를 그대로 쓴다. 변환은 Timeline 계층의 몫이다.
# 기록 질문 계약(#66): 결과의 event 마다 회고 유도 질문 하나가 붙는다(`question`). 사용자가
#   답하면 그대로 기록이 되는 질문이며, 해요체 의문문·40자 내외·event 당 1개다.
#   **모든 event 에 하나씩이고 종류에 따른 예외는 없다** — SLEEP·MOVEMENT 처럼 밋밋해 보여도
#   남길 말이 있는지는 사용자가 판단한다. 1차 응답에서 빠진 event 는 그것만 모아 한 번 더
#   묻고, 그래도 비면 null 로 두고 warning 을 남긴다(질문 하나로 저장을 막지 않는다).
#   질문 단계는 **반드시 Repair 뒤**다. Repair 가 event 를 병합·삭제하고 clientEventId 를
#   다시 매기므로 그전에 만든 질문은 사라진 event 를 가리킨다.
#   실패는 흡수한다(1209) — 질문이 없다고 하루 기록을 버리지 않는다.
#   예전의 내부 모호성 질문(TimelineDraft.questions)은 #118 에서 없앴다 — 읽어서 쓰는 곳이
#   없었다. Timeline 의 LLM 출력 계약은 TimelineAgentOutput(events·warnings)뿐이고, LLM
#   warning 은 Timeline 만 아는 판단(근거 충돌에서 고른 쪽, 일부러 쓰지 않은 근거와 이유)에
#   한한다. 코드 guard 가 남기는 것은 다시 적지 않는다.
# Timeline v3(#118): **판단 순서대로 위에서 아래로 읽힌다.** 하루 구조 → 근거로 event 구성 →
#   eventType·시간·장소 결정 → **User Memory 반영(별도 단계)** → 문장 → 최종 검증의 여섯
#   단계이고, 3단계까지는 User Memory 를 쓰지 않는다. 2·3단계의 공통 규칙은 **기본값**이며
#   타입별 절이 다르게 적으면 그 타입에서는 그 절이 이긴다(MEAL 의 시간, PHOTO_MOMENT 의 장소).
#   한 타입을 만드는 데 필요한 것(합치는 근거·보태는 근거·시간과 지속시간·장소·User Memory
#   범위·근거가 약할 때·예시)은 **그 타입의 절 하나**에 모여 있다 — 표 여러 개를 대조하지
#   않는다. 헷갈리는 타입의 경계는 3단계에 따로 둔다. 최종 event 는 10개 이내이고 description
#   에 시간 표현을 쓰지 않는다(언제는 startTime·endTime 이 담는다). 말투 규정은 v2 와 달리
#   문장을 쓰는 5단계에 있다. Event Agent 가 이미 하는 판단은 Timeline 에서 지웠다. v2 는 그대로다.
#   User Memory 필드 10개와 customAttributes 의 뜻을 입력 절에 적는다. **정본은 프로필을 쓰는
#   쪽(User Memory Agent 프롬프트)의 정의 표**이고, 읽는 쪽 문장이 그것과 같은지 테스트가 본다 —
#   다르게 적으면 같은 문장을 서로 다른 뜻으로 쓰고 읽는다. Question v3 에는 아직 넣지 않았다.
#   `REST` 는 쉬었다는 근거(쉬는 장면 사진, User Memory 의 휴식 습관)가 있을 때만 쓰고,
#   근거 없는 체류는 `UNKNOWN` 이다. 취소·변경 알림은 거의 수신되지 않아 다루지 않는다.
#   **v3 는 수면을 다루지 않는다.** 수면 기록을 정확히 받을 수 없게 돼 Timeline·Question v3 에서
#   `SLEEP`·`WAKE_UP` 의 규칙과 예시를 뺐다. Timeline 에는 "수면 기록에서 온 candidate·fragment
#   는 쓰지 않고 SLEEP·WAKE_UP event 를 만들지 않는다"는 한 문장만 남겼다 — 통째로 빼면 수면
#   기록이 든 입력에서 SLEEP event 가 되살아난다. **프롬프트만 그렇다** — `EventType` 13종
#   계약, SleepActivity Agent, sleep_guard 는 그대로라 수면 기록이 입력에 들어오면 코드는
#   여전히 그 경계를 강제한다.
# User Memory 계약(#65): 입력 조회 응답의 선택 필드 `userMemory` 는 사용자 압축 프로필
#   v1.0 이다. 전달 경로는 입력 조회 → CollectedSnapshot → normalize → TimelineDraftRequest
#   → user_memory_to_text 하나뿐이고, **Timeline·Question·Repair Agent 가 같은 문자열을
#   본다** — Agent 별로 필드를 골라 쓰거나 다시 접지 않는다.
#   **Event Agent 에는 주입하지 않는다.** Event Agent 는 자기 source 에 대한
#   사실 보고가 임무이고(#61 의 계층 경계와 같다), 다섯이 병렬로 돌며 같은 프로필을 읽으면
#   Timeline 이 그 합의를 서로 다른 source 의 독립 근거로 잘못 센다. 생활 장소명(집·회사)과
#   관계 호칭처럼 프로필이 있어야 하는 판단은 Timeline 프롬프트가 갖는다.
#   **Repair 는 v3 세트에서만 받는다(#119).** 내용이 빈 event 를 구체화하려면 Timeline 이 본
#   것과 같은 프로필을 봐야 한다. Timeline 에서 한 번, Repair 가 돌 때마다 한 번씩 싣고 쓰는
#   경계는 Timeline v3 4단계와 같다. 내용이 부족할 때만 싣는 분기는 두지 않는다 — 부족한지는
#   LLM 이 판단해야 하고 그러려면 LLM 단계가 하나 더 필요하다. v1·v2 의 Repair 는 받지 않는다.
#   **사건 데이터가 아니라 해석·표현용 보조 context 다.** User Memory 만으로 사건 발생·일정
#   참석·장소·이동 목적·실명/정확한 관계를 확정하지 않고, 수집 원본과 충돌하면 원본이 이긴다.
#   이 경계는 프롬프트가 지킨다. 결정론 코드는 자연어 필드 내용이나 customAttributes 키에
#   구조적으로 의존하지 않는다(notification_guard 는 통째 문자열 검색이라 키에 무관하다).
#   계약 위반은 흡수한다(1106) — 보조 context 하나 때문에 하루치 수집 원본을 버리지 않는다.
#   본문은 운영 로그·관측 어디에도 남기지 않는다. redact_value 가 `userMemory` 와
#   `dailyTimelines` 키를 비식별 요약(schemaVersion·채워진 필드 수·크기 / 타임라인 수·event 수·memo 수)
#   으로 바꾸므로 호출부가 스냅샷이나 요청을 통째로 덤프해도 본문이 새지 않는다.
#   Langfuse generation input(프롬프트 본문)에는 값이 들어가지만 운영은 콘텐츠 정책이 NONE 이다.
#   Repair 의 분석 관측은 프롬프트를 문자열로 통째로 싣는다. 키로 거르는 마스킹이 닿지 않아
#   그 자리에서 본문을 비식별 요약으로 바꿔 끼운다(#119).
# 프롬프트 제외 키(#80, #127): `latitude`/`longitude` 와 `photoUrl` 은 request 로 계속 받지만
#   **프롬프트에는 싣지 않는다.** 좌표는 사람이 읽고 판단할 값이 아니라 input token 만 차지하고,
#   좌표가 필요한 판단(연속 MOVEMENT 사이 끝점 거리 등)은 코드가 `derivedMetrics` 로 계산해
#   결론만 넘긴다. `photoUrl` 은 이미지가 vision 호출에 bytes 로 따로 실려 LLM 이 URL 에서
#   얻을 정보가 없다. 제외 지점은 `parsing.items_to_text_without_prompt_excluded_keys`
#   (Location·Photo Agent)와 `repair/tools._lookup_source` 다. 입력 스키마에서 필드를 없애는
#   것이 아니고 마스킹도 아니다 — `photoUrl` 은 Langfuse 요청 덤프에 원문이 남는다(사진 검증용).
#   PHOTO 는 `places`/`address` 를 받아 처음으로 장소 근거가 된다 — 다만 **안 들어올 수
#   있고**, 없으면 촬영 시각으로 STAY 를 대조하는 기존 경로가 답한다.
# 프롬프트 동결본: 활성 프롬프트를 크게 바꿀 때 같은 디렉터리에 `<활성파일명>_v<버전>.md`
#   로 직전 버전을 복사해 둔다(예: `timeline_v2.0.0.md`). load_prompt 는 정확한 파일명만
#   읽으므로 동결본은 실행에 영향이 없다. **활성 파일은 `timeline.md`·`prompt.md`·
#   `question.md` 뿐이다.**

tests/
├── agents/                    # Event Agent live 입력 테스트(opt-in)
├── api/ · services/ · main/   # 엔드포인트·정규화·App Server 연동·파이프라인 단위 테스트
├── integration/               # 실제 LLM 통합 테스트(opt-in)
└── fixtures/                  # 요청/스냅샷 빌더 + App Server 클라이언트 테스트 더블

# 배포 (#29, #90)
Dockerfile                     # amd64/arm64 공용, uv 멀티스테이지, non-root, 8080
.dockerignore                  # deny-all 후 app/·pyproject.toml·uv.lock 만 허용 (.env 유입 차단)
.github/workflows/
├── deploy-ec2.yml             # dev push → amd64 빌드 → ECR(laimory-ai) → SSM 으로 EC2 교체.
│                              #   dev 이외 브랜치에서는 수동 실행도 막는다
├── deploy-production.yml      # main push → 승인 → arm64 빌드 → ECR(laimory-ai-prod) →
│                              #   Runtime 새 버전 → 엔드포인트 전환 → 실패 시 자동 복구.
│                              #   Environment production 을 선언한 job 하나뿐이라
│                              #   승인 게이트와 전용 자격증명이 같은 자리에 있다(#90)
├── rollback-production.yml    # 수동 실행(main 에서). 엔드포인트를 이전 Runtime 버전으로
│                              #   되돌림. 재빌드 없음 — 버전이 그때의 이미지를 물고 있다
└── pr-main-guard.yml          # main 대상 PR 의 source branch 가 dev 인지 검사(#90).
                               #   ruleset 의 **필수 check 로 등록해야** 효력이 생긴다.
                               #   job name 이 곧 check 이름이라 바꾸면 보호가 조용히 풀린다
scripts/deploy-ec2.sh          # EC2 컨테이너 교체·헬스체크·실패 시 직전 이미지 복구
scripts/prune_ecr_images.py    # EC2 배포 성공 후 dev ECR 정리. **저장소 전체**를 훑어
                               #   현재·직전 태그가 없는 이미지를 지운다(아키텍처 구분 없음).
                               #   production 이 저장소를 따로 쓰는 이유가 이것이다
docs/deploy-production.md      # main→production 승격·배포·롤백, GitHub ruleset·Environment,
                               #   production OIDC·IAM·ECR 설정 절차(#90)
docs/github/main-ruleset.example.json # ruleset 적용용 예시 payload. 이 파일을 고쳐도
                               #   GitHub 설정은 바뀌지 않는다(문서의 gh 명령으로 적용)
docs/deploy-ec2.md             # EC2(개발) AWS 준비·배포·운영 절차
docs/deploy-agentcore.md       # AgentCore Runtime 계약과 AWS 자원 준비
docs/agentcore-cutover-manual.md # Runtime·엔드포인트를 처음 만들 때 사람이 하는 작업(#89)
```
