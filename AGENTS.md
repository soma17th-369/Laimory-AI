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

기본 운영 경로는 EC2 단일 컨테이너입니다. `dev` 브랜치 push 가 GitHub Actions 를
돌려 amd64 이미지를 ECR 에 올리고, Systems Manager 로 EC2 컨테이너를 교체합니다.
AgentCore Runtime 은 장애가 해소됐을 때 사용할 수동 복구 경로로 유지합니다.

- EC2 절차와 AWS 사전 준비는 [docs/deploy-ec2.md](docs/deploy-ec2.md)를 따릅니다.
- AgentCore 수동 배포·롤백은 [docs/deploy-agentcore.md](docs/deploy-agentcore.md)를 따릅니다.
- 컨테이너는 8080 포트에서 `POST /v1/timeline`, `POST /invocations`, `GET /ping` 을 제공합니다.
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
│   ├── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini/Bedrock, 확장형) + LLM 관측/토큰 emit
│   ├── inflight.py            # 진행 중 백그라운드 처리 카운터 (GET /ping 의 Healthy/HealthyBusy 판단용, 프로세스 로컬)
│   ├── secrets.py             # 시크릿 해석 (#30). 값을 읽는 유일한 자리. 환경변수/.env 가 먼저이고
│   │                          #   없으면 AWS Secrets Manager 번들(JSON 하나)에서 채운다. 대상 키 목록을
│   │                          #   코드가 갖지 않는다 — 번들에 있는 키를 쓰고 없으면 빈 값이다.
│   │                          #   조회 실패는 기동을 막지 않고 1408 로 남긴다(캐시하지 않아 회복된다)
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
│   ├── agentcore.py           # AgentCore Runtime 컨테이너 계약 (POST /invocations, GET /ping). 처리는 v1/timeline 에 위임하는 어댑터
│   ├── error_handlers.py      # 전역 예외 처리기 (#42). 검증오류/HTTPException/AppError/미처리 4종을 ErrorResponse 로 통일 + OpenAPI ERROR_RESPONSES
│   └── v1/
│       ├── router.py          # v1 라우터 취합
│       ├── timeline.py        # POST /v1/timeline (taskId+taskToken+dailyRecordId+window 접수 → 202). 상태 조회 없음(상태는 App Server 소유)
│       └── user_memory.py     # POST /v1/user-memory (#64). 확정된 하루 타임라인 접수 → 202.
│                              #   dailyTimelines 는 최대 5건. 그 안의 event 수·본문 길이는
│                              #   거절하지 않고 digest 에서 자른다
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── error.py               # 공통 오류 응답 ErrorResponse(errorCode:int, error:str)
│   ├── task.py                # TaskStatus + 완료 콜백 payload(errorCode:int|None, 성공/실패 필드 짝 강제)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 파이프라인 내부 계약
│   ├── timeline_input.py      # App Server 입력 조회 응답 계약 (#40, #65). window.startAt/endAt →
│   │                          #   CollectedSnapshot 변환. userMemory 는 원본 dict 로 느슨하게 받고
│   │                          #   parse_user_memory() 가 따로 검증한다 — 여기서 엄격히 선언하면
│   │                          #   보조 context 하나가 응답 전체를 1102 로 죽인다
│   ├── user_memory.py         # 사용자 압축 프로필 v1.0 (#65). 고정 자연어 10필드(각 200자) +
│   │                          #   customAttributes(5개·150자). extra="forbid". prompt_payload() 가
│   │                          #   projection 규칙(빈 필드·메타데이터 제외, 선언 순서)을 소유한다
│   ├── user_memory_update.py  # 갱신 접수·저장 계약 (#64). dailyTimelines 는 최대 5건이고
│   │                          #   그 안의 events[] 를 **느슨하게** 받는다
│   │                          #   (eventType 자유 문자열, endAt·subtitle·question·memo nullable,
│   │                          #   길이 상한 없음). UserMemoryResultRequest 는 status 에 따라 필드 짝
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
│   ├── user_memory/user_memory_agent.py  # User Memory 전체 갱신본 생성 (#64). append 가 아니라
│   │                          #   rewrite 다. **title·subtitle·question 은 우리 AI 가 쓴 문장이라
│   │                          #   성향 근거로 쓰지 않는다** — 그러면 모델이 자기 출력을 읽고 사용자를
│   │                          #   만들어 내는 되먹임이 된다. 성향 계열 5필드의 근거는 memo 뿐이고,
│   │                          #   memo 없는 날은 그 필드가 그대로인 것이 정상이다.
│   │                          #   타임라인 파이프라인 밖이라 base.Agent 를 상속하지 않는다
│   └── main/main_agent.py     # events → timeline → repair → question 조율(LangGraph)
│
└── services/
    ├── app_server_client.py   # App Server 서버간 API 클라이언트 (#40, #64). 입력 조회/결과 저장/콜백/
    │                          #   User Memory 결과 저장 4종을 소유.
    │                          #   TaskToken 홀더(응답 body 로 갱신, Task-Token 헤더로만 전송, 로그 금지),
    │                          #   재시도(timeout·5xx)와 중단(401/404/409) 정책의 유일한 자리
    ├── source_contract.py     # 입력 조회 응답의 묶음 계약 검증 (taskId 일치/0건/rawId 중복) + SourceBatchError
    ├── timeline_result.py     # TimelineDraft → 결과 저장 요청 변환 (subtitle←description, question 그대로,
    │                          #   rawId 디듀프, 255자 절단, tz 정렬)
    ├── timeline_validator.py  # 저장 전 자체검증 (task source 소속/시간 등)
    ├── normalizer.py          # 수집 스냅샷을 itemType별로 분리·정규화
    ├── draft_repair.py        # draft 확정 repair (아래 순서대로 조립)
    ├── validator.py           # 요청 시간 범위(window) 강제: 범위 밖 event 제거/경고
    ├── source_lookup.py       # sourceRef → 입력 항목 역참조. rawId가 정식 식별자이고,
    │                          #   LLM이 붙인 sourceType 라벨은 입력의 실제 타입으로 정정한다
    ├── sleep_guard.py         # 수면 경계 강제: 기상 이전 event 제거, 수면에 걸친 event 클램프
    ├── stay_merge.py          # 이동 없이 이어진 같은 장소 STAY 묶기 (끊긴 수집 복원, 입력만 분석)
    ├── calendar_guard.py      # timeline 에서 통째로 빠진 캘린더 일정을 event 로 복원 (누락 방지)
    ├── calendar_location.py    # 캘린더 locationText ↔ STAY place/address 일치 시 confidence 보강
    ├── meal_guard.py           # MEAL event 지속시간 20~60분 강제 (긴 체류 전체를 식사로 잡지 않음)
    ├── narrative_guard.py      # 사용자 노출 description 길이 검사 (#61). 120자 초과를 LOW
    │                           #   warning 으로 남긴다. 문체·문장 수는 재지 않는다(의미 판단)
    ├── duration_guard.py       # 비캘린더 event 지속시간 상한 검사 (#61). 3시간 초과를 LOW
    │                           #   warning 으로 남기고 **자르거나 나누지 않는다** — 어디서
    │                           #   끊을지는 Repair 의 판단이다. CALENDAR_EVENT·SLEEP·MOVEMENT
    │                           #   ·MEAL 은 제외(지속 구간이 근거에 직접 있거나 meal_guard 담당)
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
    ├── user_memory_limits.py  # 갱신 크기 정책 (#64). dailyTimelines 는 schema 에서 최대 5건,
    │                          #   그 안의 입력은 **거절하지 않고 자른다**(하루당 event 20개,
    │                          #   memo 있는 event 우선 보존). 출력은 **자르지 않고
    │                          #   지적한다**(전체 1,200자·민감정보). 지적 문장에 값을 인용하지 않는다
    ├── user_memory_repair.py  # 갱신본 확정 (#64). 위반을 붙여 재요청(기본 2회), 소진 시 문서를
    │                          #   만들지 않는다(1304). schemaVersion·updatedAt 은 서버가 박는다
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
# repair_draft 순서: sourceType 정정 → 캘린더 복원 → duration → 근거 구간 정렬 → MEAL
#   → 수면 경계 → window → 장소 확정 → 정렬 → 체류 병합 → 겹침 정리 → confidence 보강
#   → 문장 길이·event 지속시간 검사 → clientEventId 재부여
#   길이 검사 두 개는 맨 뒤여야 한다. 병합·겹침 정리로 문장과 시간이 바뀌므로 앞에 두면
#   곧 사라질 값을 재게 된다. 둘 다 Repair 반복마다 자기 이전 warning 을 지우고 다시 잰다.
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
#   이것은 TimelineDraft.questions(모호성 확인, 내부 전용)와 **다른 값**이다.
# User Memory 계약(#65): 입력 조회 응답의 선택 필드 `userMemory` 는 사용자 압축 프로필
#   v1.0 이다. 전달 경로는 입력 조회 → CollectedSnapshot → normalize → TimelineDraftRequest
#   → user_memory_to_text 하나뿐이고, **Timeline Agent 와 Question Agent 가 같은 문자열을
#   본다** — Agent 별로 필드를 골라 쓰거나 다시 접지 않는다.
#   **Event Agent 와 Repair Agent 에는 주입하지 않는다.** Event Agent 는 자기 source 에 대한
#   사실 보고가 임무이고(#61 의 계층 경계와 같다), 다섯이 병렬로 돌며 같은 프로필을 읽으면
#   Timeline 이 그 합의를 서로 다른 source 의 독립 근거로 잘못 센다. 생활 장소명(집·회사)과
#   관계 호칭처럼 프로필이 있어야 하는 판단은 Timeline 프롬프트가 갖는다. Repair 는 Timeline
#   이 이미 메모리를 보고 문장을 만든 뒤이고 반복 호출이라 제외한다.
#   **사건 데이터가 아니라 해석·표현용 보조 context 다.** User Memory 만으로 사건 발생·일정
#   참석·장소·이동 목적·실명/정확한 관계를 확정하지 않고, 수집 원본과 충돌하면 원본이 이긴다.
#   이 경계는 프롬프트가 지킨다. 결정론 코드는 자연어 필드 내용이나 customAttributes 키에
#   구조적으로 의존하지 않는다(notification_guard 는 통째 문자열 검색이라 키에 무관하다).
#   계약 위반은 흡수한다(1106) — 보조 context 하나 때문에 하루치 수집 원본을 버리지 않는다.
#   본문은 운영 로그·관측 어디에도 남기지 않는다. redact_value 가 `userMemory` 와
#   `dailyTimelines` 키를 비식별 요약(schemaVersion·채워진 필드 수·크기 / 타임라인 수·event 수·memo 수)
#   으로 바꾸므로 호출부가 스냅샷이나 요청을 통째로 덤프해도 본문이 새지 않는다.
#   Langfuse generation input(프롬프트 본문)에는 값이 들어가지만 운영은 콘텐츠 정책이 NONE 이다.
# 좌표 경계(#80): `latitude`/`longitude` 는 request 로 계속 받지만 **프롬프트에는 싣지
#   않는다.** 사람이 읽고 판단할 값이 아니라 input token 만 차지하고, 좌표가 필요한 판단
#   (연속 MOVEMENT 사이 끝점 거리 등)은 코드가 `derivedMetrics` 로 계산해 결론만 넘긴다.
#   제외 지점은 `parsing.items_to_text_without_coordinates`(Location·Photo Agent)와
#   `repair/tools._lookup_source` 다. 입력 스키마에서 필드를 없애는 것이 아니다.
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

# 배포 (#29)
Dockerfile                     # amd64/arm64 공용, uv 멀티스테이지, non-root, 8080
.dockerignore                  # deny-all 후 app/·pyproject.toml·uv.lock 만 허용 (.env 유입 차단)
.github/workflows/
├── deploy-ec2.yml             # dev push → amd64 빌드 → ECR push → SSM 으로 EC2 교체
├── deploy-agentcore.yml       # 수동 실행. arm64 빌드 → Runtime 새 버전 → 엔드포인트 전환
└── rollback-agentcore.yml     # 수동 실행. 엔드포인트를 이전 Runtime 버전으로 되돌림(재빌드 없음)
scripts/deploy-ec2.sh          # EC2 컨테이너 교체·헬스체크·실패 시 직전 이미지 복구
docs/deploy-ec2.md             # EC2 AWS 준비·배포·운영 절차
docs/deploy-agentcore.md       # AgentCore 수동 배포·롤백 절차
```
