# Laimory-AI 에이전트 지침

이 저장소는 FastAPI 기반 Python 서버 프로젝트입니다. Codex와 Claude가 같은 프로젝트 지침을 공유할 수 있도록 이 파일을 공통 기준으로 사용합니다.

## 기본 작업 방식
- 모든 md 파일은 한글을 base 로 생성합니다.
- 변경 전에는 관련 파일을 먼저 읽고 현재 구조를 기준으로 판단합니다.
- 불필요한 리팩터링이나 unrelated 변경은 하지 않습니다.
- 사용자가 명시하지 않은 파일 삭제, git reset, checkout 같은 파괴적 작업은 하지 않습니다.
- 기존 변경사항이 있으면 사용자 작업으로 보고 되돌리지 않습니다.

## Python 환경

- Python 버전은 `.python-version`과 `pyproject.toml` 기준을 따릅니다.
- 이 프로젝트는 `uv`와 `.venv`를 사용합니다.
- 의존성 설치는 프로젝트 루트에서 `uv sync`를 사용합니다.
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
│       └── timeline.py        # POST /v1/timeline (taskId+taskToken+dailyRecordId+window 접수 → 202). 상태 조회 없음(상태는 App Server 소유)
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── error.py               # 공통 오류 응답 ErrorResponse(errorCode:int, error:str)
│   ├── task.py                # TaskStatus + 완료 콜백 payload(errorCode:int|None, 성공/실패 필드 짝 강제)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 파이프라인 내부 계약
│   ├── timeline_input.py      # App Server 입력 조회 응답 계약 (#40). window.startAt/endAt → CollectedSnapshot 변환.
│   │                          #   선택적 userMemory 를 그대로 전달한다(#67) — 생활 장소명 판별용
│   ├── timeline_result.py     # App Server 결과 저장 요청 계약 (#40, #67). eventType/title/subtitle/
│   │                          #   startAt/endAt/placeLabel/address/sourceRawIds
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
│   └── main/main_agent.py     # events → timeline → repair 조율(LangGraph)
│
└── services/
    ├── app_server_client.py   # App Server 서버간 API 클라이언트 (#40). 입력 조회/결과 저장/콜백 3종을 소유.
    │                          #   TaskToken 홀더(응답 body 로 갱신, Task-Token 헤더로만 전송, 로그 금지),
    │                          #   재시도(timeout·5xx)와 중단(401/404/409) 정책의 유일한 자리
    ├── source_contract.py     # 입력 조회 응답의 묶음 계약 검증 (taskId 일치/0건/rawId 중복) + SourceBatchError
    ├── timeline_result.py     # TimelineDraft → 결과 저장 요청 변환 (subtitle←description, rawId 디듀프,
    │                          #   tz 정렬). 길이는 narrative_guard 의 30/120 을 쓰고(#67, 예전 255자
    │                          #   절단 대체), SLEEP·WAKE_UP 은 여기서도 제외한다
    ├── timeline_validator.py  # 저장 전 자체검증 (task source 소속/시간 등)
    ├── normalizer.py          # 수집 스냅샷을 itemType별로 분리·정규화
    ├── draft_repair.py        # draft 확정 repair (아래 순서대로 조립)
    ├── validator.py           # 요청 시간 범위(window) 강제: 범위 밖 event 제거/경고.
    │                          #   **조건부가 아니다**(#67). 경계를 세우지 못하면
    │                          #   WindowBoundsError 로 멈춘다 — 검증 없는 타임라인은 만들지 않는다
    ├── source_lookup.py       # sourceRef → 입력 항목 역참조. rawId가 정식 식별자이고,
    │                          #   LLM이 붙인 sourceType 라벨은 입력의 실제 타입으로 정정한다
    ├── sleep_exclusion.py     # 수면·기상 비노출 경계 (#67). SLEEP/WAKE_UP event 를 결과에서
    │                          #   빼고, 수면 의미를 지닌 rawId(SLEEP·CALENDAR)를 제외 집합으로
    │                          #   만들어 다른 event 의 근거에서도 걷어 낸다. Repair 매 패스마다
    │                          #   현재 Event Agent 결과로 다시 계산한다
    ├── sleep_guard.py         # 수면 경계 강제. **파이프라인에서 빠졌다**(#67) — 수면 기록을
    │                          #   믿을 수 없어 다른 event 를 지우고 자를 권한을 뺏었다. 정확한
    │                          #   수면 데이터가 복구될 때를 위해 독립 서비스로만 남긴다
    ├── stay_merge.py          # 이동 없이 이어진 같은 장소 STAY 묶기 (끊긴 수집 복원, 입력만 분석).
    │                          #   수면 구간은 더 이상 병합 차단 근거가 아니다(#67)
    ├── calendar_guard.py      # timeline 에서 통째로 빠진 캘린더 일정을 event 로 복원 (누락 방지)
    ├── calendar_location.py    # 캘린더 locationText ↔ STAY place/address 일치 시 confidence 보강
    ├── meal_guard.py           # MEAL event 지속시간 20~60분 강제 (긴 체류 전체를 식사로 잡지 않음)
    ├── narrative_guard.py      # 사용자 노출 문장 길이의 정본 (#61, #67). title 30자·
    │                           #   description 120자 상수를 여기 한 곳에 둔다(저장 변환도 같은
    │                           #   값을 쓴다). verify_* 는 Repair 반복 중 경고만 남기고,
    │                           #   enforce_* 는 반복이 끝난 뒤 문장→단어→하드 순으로 줄인다.
    │                           #   event 를 버리지 않는다 — 원하는 것은 삭제가 아니라 축약이다
    ├── duration_guard.py       # event 지속시간 상한 검사 (#61, #67). 3시간 초과를 LOW
    │                           #   warning 으로 남기고 **자르거나 나누지 않는다** — 어디서
    │                           #   끊을지는 Repair 의 판단이다. 면제는 **enum 이 아니라 실제
    │                           #   근거**로 판단한다(#67): CALENDAR·SLEEP sourceRef 를 정말
    │                           #   인용하는 event 만 빠지고, 라벨만 CALENDAR_EVENT 인 event 는
    │                           #   경고 대상이다. MOVEMENT·MEAL 은 종류로 제외
    ├── place_resolver.py       # placeLabel 확정, 근거 없는 address 제거. 표시 우선순위(#67):
    │                           #   User Memory 생활 장소명(집·회사·학교) → 실제 상호·건물명
    │                           #   → 정확한 주소 fallback. 근거가 없으면 만들지 않고 null
    ├── place_text.py           # 장소 문자열 정규화·비교 (calendar_location/place_resolver/stay_merge 공용)
    └── timeline_runner.py     # 백그라운드(무상태): 입력 조회→정규화→main agent→결과 저장→콜백. 최종 상태 반환

# 처리 흐름: taskId+taskToken+dailyRecordId+window 접수 → 202 즉시응답 →
#   (백그라운드) 입력 조회 API → 요청 window 를 정본으로 덮어쓰기 → normalize → main agent
#   → 저장 전 자체검증 → 결과 저장 API(200 확인) → 콜백(SUCCESS/FAILED 통보만)
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
#   앞 3개는 LLM 이 의미를 판단하는 확률적 단계, repair_draft 는 코드가 확정하는 결정론적 단계다.
# repair_draft 순서: sourceType 정정 → **수면 비노출** → 캘린더 복원 → duration
#   → 근거 구간 정렬 → MEAL → **window(무조건)** → 장소 확정 → 정렬 → 체류 병합
#   → 겹침 정리 → confidence 보강 → 문장 길이·event 지속시간 검사 → clientEventId 재부여
#   길이 검사 두 개는 맨 뒤여야 한다. 병합·겹침 정리로 문장과 시간이 바뀌므로 앞에 두면
#   곧 사라질 값을 재게 된다. 둘 다 Repair 반복마다 자기 이전 warning 을 지우고 다시 잰다.
#   수면 비노출은 맨 앞이어야 한다. 뒤의 지속시간 복원·병합이 수면 rawId 를 근거로 잡으면
#   숨기기로 한 정보가 다른 event 의 시간을 만든다.
# 수면 정책(#67): SLEEP·WAKE_UP 은 최종 결과에 없다. 화면에서만 감추는 것이 아니라 수면
#   rawId 가 다른 event 의 근거에서도 걷힌다. `enforce_sleep_boundary` 는 파이프라인과
#   Repair 도구에서 빠졌고, `stay_merge` 의 수면 차단도 없앴다. 기상 이전 event 를 막는 일은
#   window 강제가 대신한다. 제외 집합에는 수면 후보가 인용한 rawId 중 SLEEP·CALENDAR 타입만
#   넣는다 — STAY·NOTIFICATION 까지 빼면 멀쩡한 낮 event 가 함께 사라진다.
# window 계약(#67): window 는 접수 요청의 필수값이고 `TimelineDraftRequest.window` 도 필수다.
#   `endAt <= startAt` 은 접수 단계에서 422 로 거절한다. 강제는 조건부 호출이 아니며,
#   저장 직전 `timeline_validator` 가 같은 기준으로 한 번 더 본다(defense-in-depth).
#   window 경계에 기상·취침 같은 생활 의미는 부여하지 않는다 — 결과 event 가 벗어날 수 없는
#   시간 범위일 뿐이다.
# 병합 계약(#67): 같은 eventType + 시간 겹침은 필요조건이지 충분조건이 아니다. 그 위에
#   같은 장소이거나 같은 rawId 를 공유해야 병합한다. 장소를 둘 다 모르는 event 두 개는
#   `_place_key` 가 None 을 돌려주어 같은 장소로 취급되지 않는다 — 서로 다른 상대와 나눈
#   두 연락이 합쳐지던 원인이었다.
# Notification app dictionary(#67): 매칭은 appName → title → text 순서이고, 같은 필드에서는
#   더 긴 alias 가 이긴다(목록 순서 의존 제거). `사용`·`취소`·`추천`·`공지` 같은 일반 단어는
#   alias 에서 뺐다 — 억제 정책이 잘못 붙어 알림이 조용히 사라졌다. 프롬프트에는 이번 요청에서
#   실제 매치된 정책 + fallback 만 싣는다(전체 6,039자 → 카톡만 있는 날 1,339자).
# 결과 문장 계약(#61, #67): title·description 은 사용자가 읽는 일기다. 1인칭 해요체 과거형,
#   description 1~2문장 100자 내외(하드 상한 120자), title 30자 이내 명사구. 추정
#   표현(`듯해요`)을 쓰지 않는다 — 모르는 것은 헤지하지 말고 문장에서 뺀다. 불확실성은
#   confidence·inferenceLevel·uncertainty 가 담당한다.
#   **수치는 둘로 나뉜다**(#67): 분 단위 시각·지속시간·걸음 수 같은 **센서 수치**는 빼고,
#   사람 이름·대화 주제·정산 금액·인원수 같은 **사건의 의미**는 남긴다. 포괄적으로 금지하면
#   `정산 연락` 처럼 사용자가 기억할 사실까지 일반명사로 뭉개진다.
#   **이 규칙은 Timeline·Repair 에만 적용한다.** Event Agent 는 정확한 사실 보고가 임무라
#   시각·수치를 그대로 쓴다. 변환은 Timeline 계층의 몫이다.
# 프롬프트 동결본: 활성 프롬프트를 크게 바꿀 때 같은 디렉터리에 `<활성파일명>_v<버전>.md`
#   로 직전 버전을 복사해 둔다(예: `timeline_v2.0.0.md`). load_prompt 는 정확한 파일명만
#   읽으므로 동결본은 실행에 영향이 없다. **활성 파일은 `timeline.md`·`prompt.md` 뿐이다.**
# 프롬프트 계보: 1차 #56(v2.0.0) → 2차 #61(v2.1.0) → 3차 #67(v2.2.0 현재 활성).
#   `PROMPT_VERSION=v2` 라는 실행 선택자는 그대로다. v2.2.0 은 규칙을 덧붙이지 않고
#   감사·삭제·재구성한 판이다. 정적 본문 합계 23,752자 → 20,639자.
#   책임 경계: Timeline 은 사건 생성과 source 간 의미 통합, Repair 는 문제 탐지와 최소 수정,
#   Location Event Agent 는 상세 이동 근거 추출. **결정론 코드가 보장하는 것(window·수면
#   비노출·길이·rawId 무결성)은 프롬프트에서 반복하지 않는다** — Repair 프롬프트의
#   「코드가 이미 보장하는 것」 절이 그 목록이고, 도구로 되돌리려 하지 말라고 못박는다.
#   `build_timeline_prompt()` 는 동적 데이터와 짧은 수행 요청만 담는다. 시스템 정책을 다시
#   주입하면 두 지시가 어긋난다(실제로 `시간 겹침 병합`·`최초~최후 확장`을 더 세게 지시했다).

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
